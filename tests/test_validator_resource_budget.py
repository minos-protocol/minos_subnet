"""Regression tests for validator scoring-window resource guards (audit F-9).

Covers the two guards that stop a slow-config / huge-VCF cohort from
consuming the validator's scoring window:
  * the per-miner wall-clock budget handed to _run_miner_tool, and
  * the output-VCF size cap applied before hap.py.
"""

import asyncio
import importlib
import sys
import types


def _noop(*args, **kwargs):
    return None


def _import_validator_with_runtime_stubs(monkeypatch):
    """Import neurons.validator without requiring a live Bittensor runtime."""
    logging_stub = types.SimpleNamespace(
        debug=_noop,
        error=_noop,
        info=_noop,
        warning=_noop,
        set_debug=_noop,
        set_trace=_noop,
    )
    bittensor_stub = types.SimpleNamespace(
        Config=object,
        Subtensor=object,
        Wallet=object,
        config=lambda parser=None: types.SimpleNamespace(),
        logging=logging_stub,
        subtensor=object,
        wallet=object,
    )
    httpx_stub = types.SimpleNamespace(
        AsyncClient=object,
        TimeoutException=Exception,
        ConnectError=Exception,
        ReadError=Exception,
    )

    monkeypatch.setitem(sys.modules, "bittensor", bittensor_stub)
    monkeypatch.setitem(sys.modules, "bittensor_wallet", types.SimpleNamespace(Keypair=object))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=_noop))
    monkeypatch.setitem(sys.modules, "httpx", httpx_stub)
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace())
    sys.modules.pop("neurons.validator", None)
    return importlib.import_module("neurons.validator")


def _stub_validator_self(**overrides):
    """Minimal stand-in for the Validator instance used by the scoring path."""
    stub = types.SimpleNamespace(
        _scoring_cfg={"threads_per_job": 2, "mem_per_job_gb": 16, "concurrency": 2},
        wallet=types.SimpleNamespace(hotkey=types.SimpleNamespace(ss58_address="5TestHK")),
        platform_client=None,
        is_registered=False,
        score_tracker=types.SimpleNamespace(update=_noop),
    )
    for key, value in overrides.items():
        setattr(stub, key, value)
    return stub


class TestRunMinerToolJobBudget:
    def test_explicit_job_budget_reaches_template_config(self, monkeypatch, tmp_path):
        validator_module = _import_validator_with_runtime_stubs(monkeypatch)
        captured = {}

        def fake_variant_call(bam_path, reference_path, output_vcf_path, region, config):
            captured.update(config)
            return {"success": True, "variant_count": 0}

        monkeypatch.setattr(
            validator_module, "load_template",
            lambda name: types.SimpleNamespace(variant_call=fake_variant_call),
        )

        result = asyncio.run(validator_module.Validator._run_miner_tool(
            _stub_validator_self(),
            tool_name="gatk",
            tool_config={"tool": "gatk"},
            bam_path=tmp_path / "in.bam",
            ref_path=tmp_path / "ref.fa",
            output_vcf_path=tmp_path / "out.vcf.gz",
            region="chr20:1-1000",
            timeout=777,
        ))

        assert result["success"] is True
        assert captured["timeout"] == 777
        sys.modules.pop("neurons.validator", None)

    def test_no_budget_keeps_full_variant_calling_timeout(self, monkeypatch, tmp_path):
        validator_module = _import_validator_with_runtime_stubs(monkeypatch)
        captured = {}

        def fake_variant_call(bam_path, reference_path, output_vcf_path, region, config):
            captured.update(config)
            return {"success": True, "variant_count": 0}

        monkeypatch.setattr(
            validator_module, "load_template",
            lambda name: types.SimpleNamespace(variant_call=fake_variant_call),
        )

        asyncio.run(validator_module.Validator._run_miner_tool(
            _stub_validator_self(),
            tool_name="gatk",
            tool_config={"tool": "gatk"},
            bam_path=tmp_path / "in.bam",
            ref_path=tmp_path / "ref.fa",
            output_vcf_path=tmp_path / "out.vcf.gz",
            region="chr20:1-1000",
        ))

        assert captured["timeout"] == validator_module.GENOMICS_CONFIG["variant_calling_timeout"]
        sys.modules.pop("neurons.validator", None)


class TestOversizedVcfCap:
    def _run_score_single_miner(self, monkeypatch, tmp_path, variant_count,
                                job_timeout=None):
        validator_module = _import_validator_with_runtime_stubs(monkeypatch)
        happy_calls = []
        tool_calls = []

        async def fake_run_miner_tool(**kwargs):
            tool_calls.append(kwargs)
            return {"success": True, "variant_count": variant_count}

        class FakeHappyScorer:
            def score_vcf(self, **kwargs):
                happy_calls.append(kwargs)
                return None

        stub_self = _stub_validator_self(
            _run_miner_tool=fake_run_miner_tool,
            happy_scorer=FakeHappyScorer(),
        )

        kwargs = dict(
            round_id="round_f9",
            sub={"miner_hotkey": "hk_resource", "tool_name": "gatk", "tool_config": {}},
            already_scored={},
            work_dir=tmp_path,
            bam_path=tmp_path / "in.bam",
            ref_path=tmp_path / "ref.fa",
            ref_sdf_path=tmp_path / "ref.sdf",
            truth_bed_path=None,
            truth_vcf_path=tmp_path / "truth.vcf",
            region="chr20:1-1000",
            scored_hotkeys=[],
            submission_times={},
        )
        if job_timeout is not None:
            kwargs["job_timeout"] = job_timeout

        asyncio.run(validator_module.Validator._score_single_miner(stub_self, **kwargs))
        sys.modules.pop("neurons.validator", None)
        return validator_module, tool_calls, happy_calls

    def test_oversized_vcf_skips_happy_and_scores_nothing(self, monkeypatch, tmp_path):
        # Import once to read the cap, then again inside the helper (fresh module).
        cap_module = _import_validator_with_runtime_stubs(monkeypatch)
        cap = cap_module.MAX_SCORED_VCF_VARIANTS

        validator_module, tool_calls, happy_calls = self._run_score_single_miner(
            monkeypatch, tmp_path, variant_count=cap + 1,
        )
        assert len(tool_calls) == 1
        assert happy_calls == []

    def test_normal_vcf_still_reaches_happy(self, monkeypatch, tmp_path):
        validator_module, tool_calls, happy_calls = self._run_score_single_miner(
            monkeypatch, tmp_path, variant_count=50_000,
        )
        assert len(tool_calls) == 1
        assert len(happy_calls) == 1

    def test_job_budget_forwarded_to_miner_tool(self, monkeypatch, tmp_path):
        validator_module, tool_calls, happy_calls = self._run_score_single_miner(
            monkeypatch, tmp_path, variant_count=10, job_timeout=424,
        )
        assert tool_calls[0]["timeout"] == 424
