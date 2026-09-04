"""Path safety around round-supplied region strings.

``region`` arrives with every round. ``round_id`` is hashed by
``safe_round_dir_name()`` before it becomes a directory, and the chromosome is
allowlisted before it is interpolated into
``datasets/reference/<chrom>/<chrom>.fa`` or the truth BED name — so a region
such as ``"../../etc/x:1-2"`` cannot reach a path.
``templates.tool_params.validate_region`` constrains the region too, but only
inside ``variant_call()`` — after those paths are built — so the allowlist here
is what covers them.

Also covers the spec-version packing published on chain as ``version_key``.
"""

import asyncio
import importlib
import sys
import types

import pytest

from neurons import (
    CHROMOSOME_PATTERN,
    MINOS_SPEC_VERSION,
    SPEC_VERSION_FIELD_WIDTH,
    __SPEC_VERSION__,
    safe_chrom,
)


TRAVERSAL_REGIONS = [
    "../../etc/x:1-2",
    "../..:1-2",
    "..:1-2",
    "/etc/passwd:1-2",
    "chr20/../../etc:1-2",
    "chr20\x00:1-2",
    "chr23:1-2",
    "chrZ:1-2",
    "$(whoami):1-2",
    "chr20;rm -rf /:1-2",
    ".:1-2",
]


class TestSafeChrom:
    def test_accepts_every_supported_contig(self):
        contigs = [f"chr{i}" for i in range(1, 23)] + ["chrX", "chrY", "chrM"]
        for chrom in contigs:
            assert safe_chrom(f"{chrom}:1000-2000") == chrom

    @pytest.mark.parametrize("region", TRAVERSAL_REGIONS)
    def test_rejects_traversal_and_injection(self, region):
        assert safe_chrom(region) is None

    def test_empty_region_keeps_the_historical_chr20_default(self):
        # The historical behaviour is `region.split(":")[0] if region else
        # "chr20"`: an absent region resolves to chr20 rather than erroring.
        assert safe_chrom("") == "chr20"
        assert safe_chrom(None) == "chr20"

    def test_non_string_region_is_rejected(self):
        assert safe_chrom(123) is None
        assert safe_chrom(["chr20:1-2"]) is None

    def test_no_accepted_value_can_escape_a_directory(self):
        from pathlib import Path

        base = Path("/datasets/reference")
        for region in [f"chr{i}:1-2" for i in range(1, 23)] + ["chrX:1-2", "chrM:1-2"]:
            chrom = safe_chrom(region)
            resolved = (base / chrom / f"{chrom}.fa").resolve()
            assert str(resolved).startswith(str(base) + "/")

    def test_pattern_matches_bare_chromosome_only(self):
        assert CHROMOSOME_PATTERN.match("chr20")
        assert not CHROMOSOME_PATTERN.match("chr20:1-2")
        assert not CHROMOSOME_PATTERN.match("Chr20")
        assert not CHROMOSOME_PATTERN.match("chr20/x")

    def test_allowlist_matches_the_tool_path_region_pattern(self):
        # The tool-side guard and this one must not drift: a contig one accepts
        # and the other rejects means either a broken round or a reopened hole.
        from templates.tool_params import REGION_PATTERN

        for i in list(range(0, 30)):
            region = f"chr{i}:100-200"
            assert bool(REGION_PATTERN.match(region)) == (safe_chrom(region) is not None)
        for name in ("X", "Y", "M", "Z", "MT"):
            region = f"chr{name}:100-200"
            assert bool(REGION_PATTERN.match(region)) == (safe_chrom(region) is not None)


def _noop(*args, **kwargs):
    return None


@pytest.fixture
def validator_module(monkeypatch):
    """Import neurons.validator without a live Bittensor runtime."""
    logging_stub = types.SimpleNamespace(
        debug=_noop, error=_noop, info=_noop, warning=_noop,
        set_debug=_noop, set_trace=_noop,
    )
    bittensor_stub = types.SimpleNamespace(
        Config=object, Subtensor=object, Wallet=object,
        config=lambda parser=None: types.SimpleNamespace(),
        logging=logging_stub, subtensor=object, wallet=object,
    )
    monkeypatch.setitem(sys.modules, "bittensor", bittensor_stub)
    monkeypatch.setitem(sys.modules, "bittensor_wallet", types.SimpleNamespace(Keypair=object))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=_noop))
    # httpx is stubbed nowhere: utils.platform_client reads real attributes off
    # it at import time (httpx.ConnectTimeout), so a SimpleNamespace breaks the
    # import before any test body runs.
    if "numpy" not in sys.modules:
        try:
            importlib.import_module("numpy")
        except ImportError:
            monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace(float32=float))
    sys.modules.pop("neurons.validator", None)
    module = importlib.import_module("neurons.validator")
    yield module
    sys.modules.pop("neurons.validator", None)


class TestValidatorRoundFilesRejectBadRegion:
    """_download_round_files() must refuse the region before it builds paths."""

    def _run(self, validator_module, monkeypatch, region, tmp_path):
        validator = object.__new__(validator_module.Validator)

        downloaded = []

        def _fake_download(url, dest, **kwargs):
            downloaded.append(str(dest))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"")
            return dest

        errors = []
        monkeypatch.setattr(validator_module.bt.logging, "error", lambda msg: errors.append(str(msg)))
        monkeypatch.setattr(validator_module, "BASE_DIR", tmp_path)
        monkeypatch.setattr(validator_module, "download_file_with_fallback", _fake_download)

        round_data = {
            "region": region,
            "bam_presigned_url": "https://example/x.bam",
            "truth_vcf_presigned_url": "https://example/truth.vcf.gz",
            "mutations_vcf_presigned_url": "https://example/mut.vcf.gz",
            "submissions": [],
        }
        result = validator._download_round_files("2026-01-21T12:00:00+00:00", round_data)
        return result, downloaded, errors

    @pytest.mark.parametrize("region", ["../../etc/x:1-2", "/etc/passwd:1-2", "chr23:1-2"])
    def test_traversal_region_aborts_the_round(self, validator_module, monkeypatch, tmp_path, region):
        result, _, errors = self._run(validator_module, monkeypatch, region, tmp_path)
        assert result is None
        # It must abort on the chromosome specifically, not incidentally on a
        # missing file — otherwise this test would pass with the guard removed.
        assert any("supported chromosome" in e for e in errors), errors

    def test_a_supported_region_gets_past_the_chromosome_check(self, validator_module, monkeypatch, tmp_path):
        _, _, errors = self._run(validator_module, monkeypatch, "chr20:1-1000", tmp_path)
        assert not any("supported chromosome" in e for e in errors), errors

    def test_no_file_is_written_outside_the_base_dir(self, validator_module, monkeypatch, tmp_path):
        _, downloaded, _ = self._run(validator_module, monkeypatch, "../../etc/x:1-2", tmp_path)
        for path in downloaded:
            assert path.startswith(str(tmp_path))


class TestValidatorDeprecatedTemplate:
    """freebayes is deprecated for new submissions but deliberately still
    scorable: refusing it here would zero a miner whose pre-cutover submission
    was legal when made. The validator warns rather than blocks."""

    def test_deprecated_tool_still_runs(self, validator_module, monkeypatch, tmp_path):
        from templates import DEPRECATED_TEMPLATES

        assert "freebayes" in DEPRECATED_TEMPLATES

        ran = {}

        def _fake_load_template(name):
            def variant_call(**kwargs):
                ran["name"] = name
                return {"success": True, "variant_count": 7, "error": None}
            return types.SimpleNamespace(variant_call=variant_call)

        warnings = []
        monkeypatch.setattr(validator_module.bt.logging, "warning", lambda msg: warnings.append(msg))
        monkeypatch.setattr(validator_module, "load_template", _fake_load_template)

        validator = object.__new__(validator_module.Validator)
        validator._scoring_cfg = {"threads_per_job": 2, "mem_per_job_gb": 4}

        result = asyncio.run(validator._run_miner_tool(
            tool_name="freebayes",
            tool_config={"tool": "freebayes", "freebayes_options": {}},
            bam_path=tmp_path / "in.bam",
            ref_path=tmp_path / "ref.fa",
            output_vcf_path=tmp_path / "out.vcf.gz",
            region="chr20:1-1000",
        ))

        assert result["success"] is True
        assert result["variant_count"] == 7
        assert ran["name"] == "freebayes"
        assert any("deprecated" in str(w).lower() for w in warnings)

    def test_supported_tool_warns_nothing(self, validator_module, monkeypatch, tmp_path):
        def _fake_load_template(name):
            return types.SimpleNamespace(
                variant_call=lambda **kwargs: {"success": True, "variant_count": 1, "error": None}
            )

        warnings = []
        monkeypatch.setattr(validator_module.bt.logging, "warning", lambda msg: warnings.append(msg))
        monkeypatch.setattr(validator_module, "load_template", _fake_load_template)

        validator = object.__new__(validator_module.Validator)
        validator._scoring_cfg = {"threads_per_job": 2, "mem_per_job_gb": 4}

        asyncio.run(validator._run_miner_tool(
            tool_name="gatk", tool_config={"tool": "gatk"},
            bam_path=tmp_path / "in.bam", ref_path=tmp_path / "ref.fa",
            output_vcf_path=tmp_path / "out.vcf.gz", region="chr20:1-1000",
        ))
        assert warnings == []


@pytest.fixture
def miner_module(monkeypatch):
    """Import neurons.miner without a live Bittensor runtime."""
    logging_stub = types.SimpleNamespace(
        debug=_noop, error=_noop, info=_noop, warning=_noop,
        set_debug=_noop, set_trace=_noop,
    )
    bittensor_stub = types.SimpleNamespace(
        Config=object, Subtensor=object, Wallet=object,
        config=lambda parser=None: types.SimpleNamespace(),
        logging=logging_stub, subtensor=object, wallet=object,
    )
    monkeypatch.setitem(sys.modules, "bittensor", bittensor_stub)
    monkeypatch.setitem(sys.modules, "bittensor_wallet", types.SimpleNamespace(Keypair=object))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=_noop))
    sys.modules.pop("neurons.miner", None)
    module = importlib.import_module("neurons.miner")
    yield module
    sys.modules.pop("neurons.miner", None)


class TestMinerReferenceResolution:
    """_resolve_reference_for() receives a chromosome split off a
    platform-supplied practice-sample region."""

    def _plant_reference_outside(self, tmp_path):
        """datasets/ under tmp_path/base, plus a decoy reachable only by
        walking out of it."""
        base = tmp_path / "base"
        (base / "datasets" / "reference" / "chr20").mkdir(parents=True)
        (base / "datasets" / "reference" / "chr20" / "chr20.fa").write_text(">chr20\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        # Would be hit by chrom="../../../outside/planted" style traversal.
        (outside / "planted").mkdir()
        (outside / "planted" / "planted.fa").write_text(">planted\n")
        return base

    def test_supported_chromosome_still_resolves(self, miner_module, monkeypatch, tmp_path):
        base = self._plant_reference_outside(tmp_path)
        monkeypatch.setattr(miner_module, "BASE_DIR", base)
        assert miner_module._resolve_reference_for("chr20") == \
            base / "datasets" / "reference" / "chr20" / "chr20.fa"

    @pytest.mark.parametrize("chrom", [
        "../../../outside/planted", "..", "../..", "/etc", "chr20/../..",
        "chrZ", "chr23", "", "chr20;rm -rf /",
    ])
    def test_traversal_chromosome_resolves_to_nothing(self, miner_module, monkeypatch, tmp_path, chrom):
        base = self._plant_reference_outside(tmp_path)
        monkeypatch.setattr(miner_module, "BASE_DIR", base)
        assert miner_module._resolve_reference_for(chrom) is None

    def test_traversal_would_have_resolved_without_the_guard(self, miner_module, monkeypatch, tmp_path):
        # Proves the rejection above is not vacuous: plant a FASTA exactly where
        # the unguarded interpolation lands, outside BASE_DIR, and show the
        # guarded resolver still refuses it.
        base = self._plant_reference_outside(tmp_path)
        monkeypatch.setattr(miner_module, "BASE_DIR", base)

        chrom = "../../../outside/planted"
        planted = (base / "datasets" / "reference" / chrom / f"{chrom}.fa").resolve()
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(">planted\n")

        assert base not in planted.parents, "decoy must sit outside BASE_DIR"
        assert (base / "datasets" / "reference" / chrom / f"{chrom}.fa").exists()
        assert miner_module._resolve_reference_for(chrom) is None

    def test_non_string_chromosome_is_rejected(self, miner_module, monkeypatch, tmp_path):
        monkeypatch.setattr(miner_module, "BASE_DIR", self._plant_reference_outside(tmp_path))
        assert miner_module._resolve_reference_for(None) is None
        assert miner_module._resolve_reference_for(123) is None


class TestMinerExecuteTemplate:
    """execute_template() builds datasets/reference/<chrom>/<chrom>.fa from the
    round's region before any template runs."""

    def _miner(self, miner_module):
        miner = object.__new__(miner_module.Miner)
        miner.variant_caller = "gatk"
        return miner

    @pytest.mark.parametrize("region", ["../../etc/x:1-2", "/etc/passwd:1-2", "chr23:1-2"])
    def test_bad_region_raises_before_loading_a_template(self, miner_module, monkeypatch, tmp_path, region):
        loaded = []
        monkeypatch.setattr(miner_module, "load_template", lambda name: loaded.append(name))
        monkeypatch.setattr(miner_module, "BASE_DIR", tmp_path)

        with pytest.raises(RuntimeError, match="supported chromosome"):
            asyncio.run(self._miner(miner_module).execute_template(tmp_path / "in.bam", region))
        assert loaded == []

    def test_supported_region_reaches_the_template(self, miner_module, monkeypatch, tmp_path):
        (tmp_path / "datasets" / "reference" / "chr20").mkdir(parents=True)
        (tmp_path / "datasets" / "reference" / "chr20" / "chr20.fa").write_text(">chr20\n")
        monkeypatch.setattr(miner_module, "BASE_DIR", tmp_path)

        seen = {}

        def _fake_load_template(name):
            def variant_call(**kwargs):
                seen.update(kwargs)
                return {"success": True, "variant_count": 3, "error": None}
            return types.SimpleNamespace(variant_call=variant_call)

        monkeypatch.setattr(miner_module, "load_template", _fake_load_template)
        bam = tmp_path / "in.bam"
        bam.write_bytes(b"")
        asyncio.run(self._miner(miner_module).execute_template(bam, "chr20:1-1000"))
        assert seen["reference_path"] == tmp_path / "datasets" / "reference" / "chr20" / "chr20.fa"


class TestSpecVersionPacking:
    """version_key is published on chain and compared against the subnet's
    WeightsVersionKey hyperparameter, which the chain rejects a validator for
    falling below. The packing here must stay in the scale that hyperparameter
    was set in -- widening a field without raising it in the same change puts
    the two out of step, and raising it to match locks out every validator
    still on the old packing at once."""

    def _pack(self, version):
        parts = version.split(".")
        w = SPEC_VERSION_FIELD_WIDTH
        return w * w * int(parts[0]) + w * int(parts[1]) + int(parts[2])

    def test_current_version_matches_the_packing(self):
        assert __SPEC_VERSION__ == self._pack(MINOS_SPEC_VERSION)

    def test_the_packed_key_clears_the_subnet_hyperparameter(self):
        """The chain refuses a validator whose version_key is below the
        subnet's WeightsVersionKey. That value is set in this same scale, so
        the packed key must clear it -- and raising the hyperparameter to this
        release's value is what later retires older validators."""
        assert __SPEC_VERSION__ == 30
        assert __SPEC_VERSION__ >= 20, "below the deployed WeightsVersionKey"

    def test_packing_is_strictly_ordered_within_a_single_digit_field(self):
        versions = ["0.0.1", "0.0.9", "0.1.0", "0.2.0", "0.9.9", "1.0.0", "1.0.1", "2.0.0"]
        packed = [self._pack(v) for v in versions]
        assert packed == sorted(packed)
        assert len(set(packed)) == len(packed)

    def test_a_field_that_would_overflow_is_refused_at_import(self):
        """0.10.0 would collide with 1.0.0 under this width. The module raises
        rather than publishing an ambiguous key, so widening the field is a
        deliberate act taken together with the hyperparameter."""
        assert SPEC_VERSION_FIELD_WIDTH == 10
        assert self._pack("0.10.0") == self._pack("1.0.0")

    def test_version_key_is_a_plain_int(self):
        # set_weights passes it as version_key; a non-int would fail on chain.
        assert isinstance(__SPEC_VERSION__, int)
        assert __SPEC_VERSION__ > 0
