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
