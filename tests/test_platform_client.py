"""Tests for utils.platform_client — HTTPS enforcement, request signing,
infrastructure param stripping, retry logic, and exception hierarchy."""

import hashlib
import json
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from utils.platform_client import (
    PlatformClient,
    PlatformConfig,
    MinerPlatformClient,
    ValidatorPlatformClient,
    PlatformClientError,
    AuthenticationError,
    retry_async,
)
from bittensor_wallet import Keypair


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _keypair() -> Keypair:
    """Deterministic Ed25519 keypair (no blockchain needed)."""
    return Keypair.create_from_seed(bytes(range(32)).hex())


def _https_config(url: str = "https://api.theminos.ai") -> PlatformConfig:
    return PlatformConfig(base_url=url)


# ---------------------------------------------------------------------------
# HTTPS enforcement
# ---------------------------------------------------------------------------

class TestHTTPSEnforcement:
    """PlatformClient.__init__ must require HTTPS except for localhost."""

    def test_https_url_accepted(self):
        client = PlatformClient(_https_config("https://api.theminos.ai"))
        assert client.config.base_url == "https://api.theminos.ai"

    def test_http_non_localhost_raises(self):
        with pytest.raises(ValueError, match="HTTPS"):
            PlatformClient(_https_config("http://api.theminos.ai"))

    def test_http_localhost_accepted(self):
        client = PlatformClient(_https_config("http://localhost:8000"))
        assert client.config.base_url == "http://localhost:8000"

    def test_http_127_0_0_1_accepted(self):
        client = PlatformClient(_https_config("http://127.0.0.1:8000"))
        assert client.config.base_url == "http://127.0.0.1:8000"

    def test_http_ipv6_loopback_accepted(self):
        client = PlatformClient(_https_config("http://[::1]:8000"))
        assert client.config.base_url == "http://[::1]:8000"


# ---------------------------------------------------------------------------
# sign_request
# ---------------------------------------------------------------------------

class TestSignRequest:
    """PlatformClient.sign_request produces valid canonical signatures."""

    def setup_method(self):
        self.keypair = _keypair()
        self.body = {"hotkey": "5abc", "round_id": "r1", "tool_name": "gatk"}
        self.timestamp = 1700000000
        self.nonce = "deadbeef1234"

    def _canonical_message(self, method, path, body, timestamp, nonce) -> bytes:
        """Rebuild the canonical message that sign_request signs internally."""
        canonical_body = {k: v for k, v in sorted(body.items()) if k not in ("signature", "nonce")}
        body_hash = hashlib.sha256(
            json.dumps(canonical_body, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()
        return f"{method.upper()}|{path}|{body_hash}|{timestamp}|{nonce}".encode()

    def test_signature_is_valid(self):
        """sign_request returns a hex signature that verifies against the canonical message."""
        sig_hex = PlatformClient.sign_request(
            self.keypair, "POST", "/v2/submit-config",
            self.body, self.timestamp, self.nonce,
        )
        canonical = self._canonical_message(
            "POST", "/v2/submit-config", self.body, self.timestamp, self.nonce,
        )
        assert self.keypair.verify(canonical, bytes.fromhex(sig_hex))

    def test_signature_excludes_signature_and_nonce_keys(self):
        """Adding 'signature' and 'nonce' to body must not change the signed message."""
        body_extra = {
            **self.body,
            "signature": "should_be_ignored",
            "nonce": "also_ignored",
        }
        sig_hex = PlatformClient.sign_request(
            self.keypair, "POST", "/v2/submit-config",
            body_extra, self.timestamp, self.nonce,
        )
        # The canonical message should be computed WITHOUT the signature/nonce keys,
        # so verify against the canonical built from the clean body.
        canonical = self._canonical_message(
            "POST", "/v2/submit-config", self.body, self.timestamp, self.nonce,
        )
        assert self.keypair.verify(canonical, bytes.fromhex(sig_hex))

    def test_different_timestamp_produces_different_canonical(self):
        """Different timestamps yield different canonical messages (thus different valid signatures)."""
        sig1_hex = PlatformClient.sign_request(
            self.keypair, "POST", "/v2/submit-config",
            self.body, self.timestamp, self.nonce,
        )
        sig2_hex = PlatformClient.sign_request(
            self.keypair, "POST", "/v2/submit-config",
            self.body, self.timestamp + 1, self.nonce,
        )
        # sig1 must NOT verify against the canonical for timestamp+1
        canonical_t2 = self._canonical_message(
            "POST", "/v2/submit-config", self.body, self.timestamp + 1, self.nonce,
        )
        assert not self.keypair.verify(canonical_t2, bytes.fromhex(sig1_hex))
        # But sig2 must verify against canonical_t2
        assert self.keypair.verify(canonical_t2, bytes.fromhex(sig2_hex))

    def test_different_body_produces_different_canonical(self):
        """Different bodies yield different canonical messages."""
        sig1_hex = PlatformClient.sign_request(
            self.keypair, "POST", "/v2/submit-config",
            self.body, self.timestamp, self.nonce,
        )
        modified_body = {**self.body, "tool_name": "deepvariant"}
        sig2_hex = PlatformClient.sign_request(
            self.keypair, "POST", "/v2/submit-config",
            modified_body, self.timestamp, self.nonce,
        )
        # sig1 must NOT verify against the canonical for modified_body
        canonical_mod = self._canonical_message(
            "POST", "/v2/submit-config", modified_body, self.timestamp, self.nonce,
        )
        assert not self.keypair.verify(canonical_mod, bytes.fromhex(sig1_hex))
        assert self.keypair.verify(canonical_mod, bytes.fromhex(sig2_hex))

    def test_body_keys_sorted_for_canonical_form(self):
        """Key insertion order must not matter — canonical form sorts keys."""
        body_a = {"z_key": 1, "a_key": 2}
        body_b = {"a_key": 2, "z_key": 1}
        sig_a_hex = PlatformClient.sign_request(
            self.keypair, "POST", "/path", body_a, self.timestamp, self.nonce,
        )
        # sig_a must verify against canonical built from body_b (same logical body)
        canonical_b = self._canonical_message(
            "POST", "/path", body_b, self.timestamp, self.nonce,
        )
        assert self.keypair.verify(canonical_b, bytes.fromhex(sig_a_hex))


# ---------------------------------------------------------------------------
# Infrastructure param stripping
# ---------------------------------------------------------------------------

class TestInfraParamStripping:
    """MinerPlatformClient.submit_config strips infra params before sending."""

    INFRA_PARAMS = {"threads", "memory_gb", "timeout", "ref_build", "num_threads"}

    def _make_client(self) -> MinerPlatformClient:
        kp = _keypair()
        return MinerPlatformClient(kp, _https_config())

    @pytest.mark.asyncio
    async def test_all_infra_params_stripped(self):
        client = self._make_client()
        tool_config = {
            "threads": 8,
            "memory_gb": 16,
            "timeout": 3600,
            "ref_build": "hg38",
            "num_threads": 4,
        }

        with patch.object(client, "_get_client") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True, "submission_id": "abc"}

            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_get.return_value = mock_http

            await client.submit_config("round1", "gatk", tool_config)

            call_kwargs = mock_http.post.call_args
            sent_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            sent_tool_config = sent_body["tool_config"]
            for param in self.INFRA_PARAMS:
                assert param not in sent_tool_config

    @pytest.mark.asyncio
    async def test_quality_params_preserved(self):
        client = self._make_client()
        tool_config = {
            "threads": 8,
            "min_base_quality_score": 20,
            "ploidy": 2,
            "stand_call_conf": 30.0,
        }

        with patch.object(client, "_get_client") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True, "submission_id": "abc"}

            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_get.return_value = mock_http

            await client.submit_config("round1", "gatk", tool_config)

            call_kwargs = mock_http.post.call_args
            sent_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            sent_tool_config = sent_body["tool_config"]
            assert sent_tool_config["min_base_quality_score"] == 20
            assert sent_tool_config["ploidy"] == 2
            assert sent_tool_config["stand_call_conf"] == 30.0

    @pytest.mark.asyncio
    async def test_empty_config_gives_empty_safe_config(self):
        client = self._make_client()

        with patch.object(client, "_get_client") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True, "submission_id": "abc"}

            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_get.return_value = mock_http

            await client.submit_config("round1", "gatk", {})

            call_kwargs = mock_http.post.call_args
            sent_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert sent_body["tool_config"] == {}

    @pytest.mark.asyncio
    async def test_mixed_infra_and_quality_only_quality_remains(self):
        client = self._make_client()
        tool_config = {
            "threads": 8,
            "memory_gb": 32,
            "min_base_quality_score": 20,
            "timeout": 7200,
            "ploidy": 2,
            "num_threads": 16,
            "ref_build": "hg38",
            "stand_call_conf": 30.0,
        }

        with patch.object(client, "_get_client") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True, "submission_id": "abc"}

            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_get.return_value = mock_http

            await client.submit_config("round1", "gatk", tool_config)

            call_kwargs = mock_http.post.call_args
            sent_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            sent_tool_config = sent_body["tool_config"]
            assert sent_tool_config == {
                "min_base_quality_score": 20,
                "ploidy": 2,
                "stand_call_conf": 30.0,
            }


# ---------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------

class TestRetryAsync:
    """retry_async honours retryable vs non-retryable exceptions."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        func = AsyncMock(return_value="ok")
        result = await retry_async(func, max_retries=3, base_delay=0)
        assert result == "ok"
        assert func.call_count == 1

    @pytest.mark.asyncio
    async def test_success_on_second_try_after_retryable(self):
        func = AsyncMock(
            side_effect=[httpx.TimeoutException("timeout"), "ok"],
        )
        result = await retry_async(func, max_retries=3, base_delay=0)
        assert result == "ok"
        assert func.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_raises_last_exception(self):
        exc = httpx.TimeoutException("timeout")
        func = AsyncMock(side_effect=exc)
        with pytest.raises(httpx.TimeoutException, match="timeout"):
            await retry_async(func, max_retries=2, base_delay=0)
        # 1 initial + 2 retries = 3 calls
        assert func.call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_exception_raises_immediately(self):
        func = AsyncMock(side_effect=ValueError("bad value"))
        with pytest.raises(ValueError, match="bad value"):
            await retry_async(func, max_retries=3, base_delay=0)
        assert func.call_count == 1


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class TestExceptions:
    """Verify the custom exception hierarchy."""

    def test_platform_client_error_is_exception(self):
        assert issubclass(PlatformClientError, Exception)
        err = PlatformClientError("something broke")
        assert isinstance(err, Exception)

    def test_authentication_error_is_platform_client_error(self):
        assert issubclass(AuthenticationError, PlatformClientError)
        err = AuthenticationError("bad sig")
        assert isinstance(err, PlatformClientError)


# ---------------------------------------------------------------------------
# Demo mode path routing
# ---------------------------------------------------------------------------

class TestDemoModeRouting:
    """MinerPlatformClient(demo=True) routes to /v2/demo/* endpoints.

    Live (demo=False, the default) preserves the original /v2/* paths so
    existing callers see no behavior change. Path selection happens at the
    method call site via the ``_round_status_path`` / ``_submit_path``
    properties; since the canonical signature includes the path, a demo
    request is cryptographically distinct from a live request even when
    the body is otherwise identical.
    """

    def _make_client(self, demo: bool) -> MinerPlatformClient:
        return MinerPlatformClient(_keypair(), _https_config(), demo=demo)

    def test_default_is_live(self):
        client = MinerPlatformClient(_keypair(), _https_config())
        assert client.demo is False
        assert client._round_status_path == "/v2/round-status"
        assert client._submit_path == "/v2/submit-config"

    def test_demo_flag_flips_paths(self):
        client = self._make_client(demo=True)
        assert client.demo is True
        assert client._round_status_path == "/v2/demo/round-status"
        assert client._submit_path == "/v2/demo/submit-result"

    @staticmethod
    def _mock_http(response_payload):
        """Build an AsyncMock httpx client that returns the given JSON payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_payload

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        return mock_http

    @pytest.mark.asyncio
    async def test_get_round_status_posts_to_live_path_when_not_demo(self):
        client = self._make_client(demo=False)
        with patch.object(client, "_get_client") as mock_get:
            mock_get.return_value = self._mock_http({"has_active_round": False})
            await client.get_round_status()
            posted_path = mock_get.return_value.post.call_args.args[0]
            assert posted_path == "/v2/round-status"

    @pytest.mark.asyncio
    async def test_get_round_status_posts_to_demo_path_when_demo(self):
        client = self._make_client(demo=True)
        with patch.object(client, "_get_client") as mock_get:
            mock_get.return_value = self._mock_http({"has_active_round": False})
            await client.get_round_status()
            posted_path = mock_get.return_value.post.call_args.args[0]
            assert posted_path == "/v2/demo/round-status"

    @pytest.mark.asyncio
    async def test_submit_config_posts_to_live_path_when_not_demo(self):
        client = self._make_client(demo=False)
        with patch.object(client, "_get_client") as mock_get:
            mock_get.return_value = self._mock_http({"success": True, "submission_id": "x"})
            await client.submit_config("round1", "gatk", {})
            posted_path = mock_get.return_value.post.call_args.args[0]
            assert posted_path == "/v2/submit-config"

    @pytest.mark.asyncio
    async def test_submit_config_posts_to_demo_path_when_demo(self):
        client = self._make_client(demo=True)
        with patch.object(client, "_get_client") as mock_get:
            mock_get.return_value = self._mock_http(
                {"success": True, "submission_id": "demo", "is_demo": True}
            )
            await client.submit_config("round1", "gatk", {})
            posted_path = mock_get.return_value.post.call_args.args[0]
            assert posted_path == "/v2/demo/submit-result"

    @pytest.mark.asyncio
    async def test_demo_signature_is_bound_to_demo_path(self):
        """Same body, demo vs live → different signatures (path-bound auth).

        Guarantees a captured live-mode signature can't be replayed against
        a demo endpoint and vice versa.
        """
        live = self._make_client(demo=False)
        demo = self._make_client(demo=True)
        captured = {}

        async def _capture(client, label):
            with patch.object(client, "_get_client") as mock_get:
                mock_get.return_value = self._mock_http({"has_active_round": False})
                await client.get_round_status()
                body = mock_get.return_value.post.call_args.kwargs.get("json") or {}
                captured[label] = (
                    mock_get.return_value.post.call_args.args[0],
                    body.get("signature"),
                )

        await _capture(live, "live")
        await _capture(demo, "demo")

        live_path, live_sig = captured["live"]
        demo_path, demo_sig = captured["demo"]
        assert live_path != demo_path
        assert live_sig and demo_sig
        assert live_sig != demo_sig, "demo + live must produce distinct signatures"


# ---------------------------------------------------------------------------
# Validator canonical-ranking: signed POST migration (commit-reveal lockdown)
# ---------------------------------------------------------------------------

class TestValidatorCanonicalRankingPost:
    """ValidatorPlatformClient.get_canonical_ranking uses the signed POST
    /v2/canonical-ranking, with a graceful fallback to the legacy public GET so
    the tiebreak signal survives the rollout."""

    def _make_client(self) -> ValidatorPlatformClient:
        return ValidatorPlatformClient(_keypair(), _https_config())

    @staticmethod
    def _mock_http(post_response=None, get_response=None):
        mock_http = AsyncMock()
        if post_response is not None:
            mock_http.post = AsyncMock(return_value=post_response)
        if get_response is not None:
            mock_http.get = AsyncMock(return_value=get_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        return mock_http

    @staticmethod
    def _resp(status_code, payload=None):
        r = MagicMock()
        r.status_code = status_code
        r.json.return_value = payload or {}
        return r

    @pytest.mark.asyncio
    async def test_signed_post_to_v2_route(self):
        client = self._make_client()
        payload = {"ranking": [{"miner_hotkey": "hk_win"}], "validator_count": 3}
        mock_http = self._mock_http(post_response=self._resp(200, payload))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_canonical_ranking(round_id="r1", top_n=5)

        assert out == payload
        args, kwargs = mock_http.post.call_args
        assert args[0] == "/v2/canonical-ranking"          # POST on the /v2 router
        body = kwargs["json"]
        # Auth-gated: carries the validator's hotkey + a fresh v2 signature/nonce.
        assert body["validator_hotkey"] == client.keypair.ss58_address
        assert body["round_id"] == "r1" and body["top_n"] == 5
        assert body["signature"] and body["nonce"] and body["timestamp"]
        assert kwargs["headers"]["X-Minos-Auth-Version"] == "2"

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_get_on_404(self):
        client = self._make_client()
        legacy_payload = {"ranking": [{"miner_hotkey": "hk_legacy"}]}
        mock_http = self._mock_http(
            post_response=self._resp(404),
            get_response=self._resp(200, legacy_payload),
        )

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_canonical_ranking(round_id="r1")

        assert out == legacy_payload
        # Fallback hit the legacy public GET on the /scoring router.
        assert mock_http.get.call_args[0][0] == "/scoring/canonical-ranking"

    @pytest.mark.asyncio
    async def test_retries_on_425_with_fresh_nonce_then_none(self):
        client = self._make_client()
        mock_http = self._mock_http(post_response=self._resp(425))

        with patch.object(client, "_get_client", return_value=mock_http), \
                patch("utils.platform_client.asyncio.sleep", new=AsyncMock()):
            out = await client.get_canonical_ranking()

        assert out is None
        assert mock_http.post.call_count == 3          # retried up to the cap
        nonces = [c.kwargs["json"]["nonce"] for c in mock_http.post.call_args_list]
        assert len(set(nonces)) == 3                   # re-signed each attempt (no replay)

    @pytest.mark.asyncio
    async def test_transport_error_degrades_to_get(self):
        client = self._make_client()
        legacy_payload = {"ranking": []}
        mock_http = self._mock_http(get_response=self._resp(200, legacy_payload))
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_canonical_ranking()

        assert out == legacy_payload


# ---------------------------------------------------------------------------
# Validator practice manifest (practice/live overlap guard support)
# ---------------------------------------------------------------------------

class TestValidatorPracticeManifest:
    """ValidatorPlatformClient.get_practice_manifest collects every *_sha256
    hash from the practice sample list and degrades to None when the
    manifest is unavailable, so the validator can hard-fail rounds that
    reuse practice material without stalling scoring on deployments where
    practice mode is disabled or the endpoint is not deployed yet."""

    PRACTICE_BAM_SHA = "a" * 64
    PRACTICE_TRUTH_SHA = "b" * 64
    PRACTICE_MUTATIONS_SHA = "c" * 64

    def _make_client(self) -> ValidatorPlatformClient:
        return ValidatorPlatformClient(_keypair(), _https_config())

    @staticmethod
    def _mock_http(post_response):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=post_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        return mock_http

    @staticmethod
    def _resp(status_code, payload=None):
        r = MagicMock()
        r.status_code = status_code
        r.text = "stub"
        r.json.return_value = payload or {}
        return r

    def _manifest_payload(self):
        return {
            "samples": [
                {
                    "sample_id": "practice_chr18_1",
                    "chromosome": "chr18",
                    "region": "chr18:1-1000000",
                    "bam_sha256": self.PRACTICE_BAM_SHA,
                    "truth_vcf_sha256": self.PRACTICE_TRUTH_SHA,
                    "mutations_vcf_sha256": self.PRACTICE_MUTATIONS_SHA,
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_signed_post_to_practice_manifest_route(self):
        client = self._make_client()
        mock_http = self._mock_http(self._resp(200, self._manifest_payload()))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_practice_manifest()

        assert out  # parsed (contents asserted below)
        args, kwargs = mock_http.post.call_args
        assert args[0] == "/v2/practice/manifest"
        body = kwargs["json"]
        # Validator-auth-gated: carries the hotkey + a fresh v2 signature/nonce.
        assert body["validator_hotkey"] == client.keypair.ss58_address
        assert body["signature"] and body["nonce"] and body["timestamp"]
        assert kwargs["headers"]["X-Minos-Auth-Version"] == "2"

    @pytest.mark.asyncio
    async def test_collects_every_sha256_field(self):
        client = self._make_client()
        mock_http = self._mock_http(self._resp(200, self._manifest_payload()))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_practice_manifest()

        assert out == {
            self.PRACTICE_BAM_SHA,
            self.PRACTICE_TRUTH_SHA,
            self.PRACTICE_MUTATIONS_SHA,
        }

    @pytest.mark.asyncio
    async def test_collects_hashes_from_multiple_samples(self):
        client = self._make_client()
        second_bam_sha = "d" * 64
        payload = self._manifest_payload()
        payload["samples"].append(
            {"sample_id": "practice_chr22_1", "bam_sha256": second_bam_sha}
        )
        mock_http = self._mock_http(self._resp(200, payload))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_practice_manifest()

        assert out == {
            self.PRACTICE_BAM_SHA,
            self.PRACTICE_TRUTH_SHA,
            self.PRACTICE_MUTATIONS_SHA,
            second_bam_sha,
        }

    @pytest.mark.asyncio
    async def test_lowercases_hashes_for_case_insensitive_matching(self):
        client = self._make_client()
        payload = {"samples": [{"sample_id": "s1", "bam_sha256": "A" * 64}]}
        mock_http = self._mock_http(self._resp(200, payload))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_practice_manifest()

        assert out == {"a" * 64}

    @pytest.mark.asyncio
    async def test_malformed_payload_gives_empty_set(self):
        client = self._make_client()
        for payload in ({}, {"samples": "not a list"}, {"samples": [{"sample_id": "s1"}]}):
            mock_http = self._mock_http(self._resp(200, payload))
            with patch.object(client, "_get_client", return_value=mock_http):
                out = await client.get_practice_manifest()
            assert out == set(), f"expected empty set for payload {payload!r}"

    @pytest.mark.asyncio
    async def test_404_returns_none(self):
        """Practice mode disabled / endpoint not deployed yet → None, not raise."""
        client = self._make_client()
        mock_http = self._mock_http(self._resp(404))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_practice_manifest()

        assert out is None

    @pytest.mark.asyncio
    async def test_server_error_returns_none(self):
        client = self._make_client()
        mock_http = self._mock_http(self._resp(500))

        with patch.object(client, "_get_client", return_value=mock_http):
            out = await client.get_practice_manifest()

        assert out is None

    @pytest.mark.asyncio
    async def test_transport_error_returns_none_after_retries(self):
        client = self._make_client()
        mock_http = self._mock_http(None)
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

        with patch.object(client, "_get_client", return_value=mock_http), \
                patch("utils.platform_client.asyncio.sleep", new=AsyncMock()):
            out = await client.get_practice_manifest()

        assert out is None
        assert mock_http.post.call_count == 3  # 1 initial + 2 retries
