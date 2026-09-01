"""Hardening tests for utils.platform_client.

Covers three defects:
  (a) the HTTPS guard was a raw string-prefix match, so URLs whose real host
      was attacker-controlled passed as "localhost";
  (b) score_id, which comes from the submit-score RESPONSE, was interpolated
      into an S3 key without sanitisation;
  (c) retry-on-timeout applied to non-idempotent POSTs, so a ReadTimeout on a
      request the server had already committed caused a double submission.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from utils.platform_client import (
    PlatformClient,
    PlatformClientError,
    PlatformConfig,
    ValidatorPlatformClient,
    MinerPlatformClient,
    retry_async,
    safe_object_id,
)
from bittensor_wallet import Keypair


def _keypair() -> Keypair:
    return Keypair.create_from_seed(bytes(range(32)).hex())


# ---------------------------------------------------------------------------
# (a) HTTPS guard must resolve the real host, not match a prefix
# ---------------------------------------------------------------------------

class TestHTTPSGuardHostParsing:

    @pytest.mark.parametrize("url", [
        # Real host is localhost.attacker.tld — a prefix match on
        # "http://localhost" would treat this as loopback and allow cleartext.
        "http://localhost.attacker.tld/v2",
        "http://127.0.0.1.attacker.tld/v2",
        # "127.0.0.1:8000" here is USERINFO — the real host is attacker.tld.
        "http://127.0.0.1:8000@attacker.tld/v2",
        "http://localhost@attacker.tld/v2",
        "http://[::1]@attacker.tld/v2",
        # Plain non-loopback http, and non-http schemes.
        "http://api.theminos.ai",
        "ftp://api.theminos.ai",
        "api.theminos.ai",
    ])
    def test_rejected(self, url):
        with pytest.raises(ValueError, match="HTTPS"):
            PlatformClient(PlatformConfig(base_url=url))

    @pytest.mark.parametrize("url", [
        "https://api.theminos.ai",
        "https://api.theminos.ai/",
        # userinfo on an https URL is still encrypted, so it stays allowed
        "https://user@api.theminos.ai",
        "http://localhost:8000",
        "http://LOCALHOST:8000",
        "http://localhost",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ])
    def test_accepted(self, url):
        client = PlatformClient(PlatformConfig(base_url=url))
        assert client.config.base_url == url


# ---------------------------------------------------------------------------
# (b) score_id from the submit-score response must not shape the S3 key
# ---------------------------------------------------------------------------

class TestSafeObjectId:

    @pytest.mark.parametrize("value", [
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "12345",
        "score_abc-1",
    ])
    def test_accepts_plausible_ids(self, value):
        assert safe_object_id(value, "score_id") == value

    @pytest.mark.parametrize("value", [
        "../../other-hotkey/pwned",
        "a/b",
        "a?versionId=1",
        "a#frag",
        "",
        "x" * 65,
        "a b",
        "a%2fb",
    ])
    def test_rejects_key_shaping_values(self, value):
        with pytest.raises(PlatformClientError, match="score_id"):
            safe_object_id(value, "score_id")


class TestSubmitVariantResultsKey:
    """submit_variant_results must refuse to build a key from a hostile score_id."""

    def _client(self):
        return ValidatorPlatformClient(
            _keypair(), PlatformConfig(base_url="https://api.theminos.ai")
        )

    def test_traversal_score_id_never_reaches_upload(self):
        client = self._client()
        with patch.object(
            client, "get_upload_url", new=AsyncMock()
        ) as get_url, patch.object(
            client, "_put_bytes_to_presigned", new=AsyncMock(return_value=True)
        ) as put:
            with pytest.raises(PlatformClientError, match="score_id"):
                asyncio.run(client.submit_variant_results(
                    score_id="../../../attacker/evil",
                    round_id="2026-08-30T00:00:00",
                    results=[],
                ))
        # No presigned URL requested and nothing uploaded.
        get_url.assert_not_awaited()
        put.assert_not_awaited()

    def test_valid_score_id_stays_under_scoring_prefix(self):
        client = self._client()
        score_id = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        get_url = AsyncMock(return_value="https://s3.example/presigned")
        post_response = MagicMock(status_code=200)
        post_response.json.return_value = {"success": True}
        http_client = MagicMock()
        http_client.post = AsyncMock(return_value=post_response)
        http_client.__aenter__ = AsyncMock(return_value=http_client)
        http_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "get_upload_url", new=get_url), \
             patch.object(client, "_put_bytes_to_presigned",
                          new=AsyncMock(return_value=True)), \
             patch.object(client, "_get_client", return_value=http_client):
            asyncio.run(client.submit_variant_results(
                score_id=score_id,
                round_id="2026-08-30T00:00:00",
                results=[{"chrom": "chr20", "pos": 1}],
            ))

        s3_key = get_url.await_args.args[0]
        assert s3_key.startswith(f"scoring/{client.keypair.ss58_address}/round_")
        assert s3_key.endswith(f"/variant_results_{score_id}.ndjson.gz")
        assert ".." not in s3_key


# ---------------------------------------------------------------------------
# (c) non-idempotent POSTs must not be retried after the bytes were sent
# ---------------------------------------------------------------------------

class TestRetryIdempotency:

    def test_idempotent_default_still_retries_read_timeout(self):
        func = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), "ok"])
        assert asyncio.run(retry_async(func, max_retries=3, base_delay=0)) == "ok"
        assert func.await_count == 2

    def test_non_idempotent_does_not_retry_read_timeout(self):
        func = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), "ok"])
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(retry_async(
                func, max_retries=3, base_delay=0, idempotent=False,
            ))
        assert func.await_count == 1

    def test_non_idempotent_does_not_retry_read_error(self):
        func = AsyncMock(side_effect=[httpx.ReadError("reset"), "ok"])
        with pytest.raises(httpx.ReadError):
            asyncio.run(retry_async(
                func, max_retries=3, base_delay=0, idempotent=False,
            ))
        assert func.await_count == 1

    @pytest.mark.parametrize("exc", [
        httpx.ConnectTimeout("no route"),
        httpx.ConnectError("refused"),
        httpx.PoolTimeout("no slot"),
    ])
    def test_non_idempotent_still_retries_pre_send_failures(self, exc):
        """These fail before any byte reaches the server, so a retry is safe."""
        func = AsyncMock(side_effect=[exc, "ok"])
        result = asyncio.run(retry_async(
            func, max_retries=3, base_delay=0, idempotent=False,
        ))
        assert result == "ok"
        assert func.await_count == 2


def _timeout_then_ok(json_payload):
    """POST side effect: ReadTimeout first, then a 200 — the double-submit shape."""
    response = MagicMock(status_code=200)
    response.json.return_value = json_payload
    return AsyncMock(side_effect=[httpx.ReadTimeout("server was slow"), response])


def _fake_http_client(post_mock):
    http_client = MagicMock()
    http_client.post = post_mock
    http_client.__aenter__ = AsyncMock(return_value=http_client)
    http_client.__aexit__ = AsyncMock(return_value=False)
    return http_client


class TestNoDoubleSubmitOnReadTimeout:
    """A ReadTimeout means the server may already have committed the POST."""

    def test_submit_config_posts_once(self):
        client = MinerPlatformClient(
            _keypair(), PlatformConfig(base_url="https://api.theminos.ai")
        )
        post = _timeout_then_ok({"success": True, "submission_id": "s1"})
        with patch.object(client, "_get_client",
                          return_value=_fake_http_client(post)):
            with pytest.raises(httpx.ReadTimeout):
                asyncio.run(client.submit_config(
                    round_id="2026-08-30T00:00:00",
                    tool_name="gatk",
                    tool_config={"min_base_quality_score": 13},
                ))
        assert post.await_count == 1

    def test_submit_score_posts_once(self):
        client = ValidatorPlatformClient(
            _keypair(), PlatformConfig(base_url="https://api.theminos.ai")
        )
        post = _timeout_then_ok({"success": True, "score_id": "abc"})
        with patch.object(client, "_get_client",
                          return_value=_fake_http_client(post)):
            with pytest.raises(httpx.ReadTimeout):
                asyncio.run(client.submit_score(
                    round_id="2026-08-30T00:00:00",
                    miner_hotkey="5FminerHotkey",
                    snp_f1=0.99,
                ))
        assert post.await_count == 1

    def test_idempotent_read_endpoint_still_retries(self):
        """get_round_status is a read; retrying it costs nothing."""
        client = MinerPlatformClient(
            _keypair(), PlatformConfig(base_url="https://api.theminos.ai")
        )
        post = _timeout_then_ok({"has_active_round": False})
        with patch.object(client, "_get_client",
                          return_value=_fake_http_client(post)):
            result = asyncio.run(client.get_round_status())
        assert result == {"has_active_round": False}
        assert post.await_count == 2
