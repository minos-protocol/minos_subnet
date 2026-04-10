"""
Tests for templates.tool_params — region validation, round_id validation,
and tool-option whitelist enforcement with flag building.
"""

from templates.tool_params import validate_region, validate_round_id, validate_and_build_flags


# ---------------------------------------------------------------------------
# Region validation
# ---------------------------------------------------------------------------

class TestValidateRegion:
    """~20 tests covering valid regions, format errors, injection, and bounds."""

    # -- Valid regions --

    def test_valid_chr20_region(self):
        result = validate_region("chr20:10000000-15000000")
        assert result["valid"] is True
        assert result["error"] is None

    def test_valid_chr1_small_region(self):
        result = validate_region("chr1:1-100")
        assert result["valid"] is True
        assert result["error"] is None

    def test_valid_chrX(self):
        result = validate_region("chrX:100-200")
        assert result["valid"] is True
        assert result["error"] is None

    def test_valid_chrY(self):
        result = validate_region("chrY:500-600")
        assert result["valid"] is True
        assert result["error"] is None

    def test_valid_chrM(self):
        result = validate_region("chrM:1-1000")
        assert result["valid"] is True
        assert result["error"] is None

    # -- Invalid format --

    def test_invalid_chr0(self):
        result = validate_region("chr0:1-100")
        assert result["valid"] is False
        assert "Invalid region format" in result["error"]

    def test_invalid_chr23(self):
        result = validate_region("chr23:1-100")
        assert result["valid"] is False
        assert "Invalid region format" in result["error"]

    def test_no_coordinates(self):
        result = validate_region("chr20")
        assert result["valid"] is False
        assert "Invalid region format" in result["error"]

    def test_empty_string(self):
        result = validate_region("")
        assert result["valid"] is False
        assert "non-empty string" in result["error"]

    def test_none_input(self):
        result = validate_region(None)
        assert result["valid"] is False
        assert "non-empty string" in result["error"]

    def test_integer_input(self):
        result = validate_region(123)
        assert result["valid"] is False
        assert "non-empty string" in result["error"]

    def test_non_numeric_coordinates(self):
        result = validate_region("chr20:abc-def")
        assert result["valid"] is False
        assert "Invalid region format" in result["error"]

    # -- Injection attempts --

    def test_injection_semicolon(self):
        result = validate_region("chr20:1-100; rm -rf /")
        assert result["valid"] is False

    def test_injection_ampersand(self):
        result = validate_region("chr20:1-100 && cat /etc/passwd")
        assert result["valid"] is False

    def test_injection_newline(self):
        result = validate_region("chr20:1-100\nmalicious")
        assert result["valid"] is False

    def test_injection_command_substitution(self):
        result = validate_region("$(whoami):1-100")
        assert result["valid"] is False

    # -- Bounds --

    def test_start_equals_end(self):
        result = validate_region("chr20:100-100")
        assert result["valid"] is False
        assert "start" in result["error"] and "less than" in result["error"]

    def test_start_greater_than_end(self):
        result = validate_region("chr20:200-100")
        assert result["valid"] is False
        assert "start" in result["error"]

    def test_region_exceeds_one_billion(self):
        result = validate_region("chr1:1-1000000002")
        assert result["valid"] is False
        assert "too large" in result["error"].lower() or "Region too large" in result["error"]

    def test_string_too_long(self):
        long_region = "chr1:" + "1" * 50 + "-" + "2" * 50
        assert len(long_region) > 100
        result = validate_region(long_region)
        assert result["valid"] is False
        assert "too long" in result["error"]


# ---------------------------------------------------------------------------
# Round-id validation
# ---------------------------------------------------------------------------

class TestValidateRoundId:
    """~12 tests covering ISO-8601 parsing, timezone requirement, and edge cases."""

    def test_valid_with_utc_offset(self):
        result = validate_round_id("2026-01-21T12:00:00+00:00")
        assert result["valid"] is True
        assert result["error"] is None

    def test_valid_with_microseconds(self):
        result = validate_round_id("2026-01-21T12:00:00.000000+00:00")
        assert result["valid"] is True
        assert result["error"] is None

    def test_valid_positive_offset(self):
        result = validate_round_id("2026-06-15T08:30:00+05:30")
        assert result["valid"] is True
        assert result["error"] is None

    def test_no_timezone_rejected(self):
        result = validate_round_id("2026-01-21T12:00:00")
        assert result["valid"] is False
        assert "timezone" in result["error"].lower()

    def test_path_traversal(self):
        result = validate_round_id("../../etc/passwd")
        assert result["valid"] is False

    def test_empty_string(self):
        result = validate_round_id("")
        assert result["valid"] is False
        assert "non-empty string" in result["error"]

    def test_none_input(self):
        result = validate_round_id(None)
        assert result["valid"] is False
        assert "non-empty string" in result["error"]

    def test_integer_input(self):
        result = validate_round_id(123)
        assert result["valid"] is False
        assert "non-empty string" in result["error"]

    def test_too_long(self):
        long_id = "2026-01-21T12:00:00+00:00" + "x" * 20
        assert len(long_id) > 40
        result = validate_round_id(long_id)
        assert result["valid"] is False
        assert "too long" in result["error"]

    def test_invalid_iso_string(self):
        result = validate_round_id("not-a-date")
        assert result["valid"] is False
        assert "not a valid ISO-8601" in result["error"]

    def test_date_only_no_time(self):
        result = validate_round_id("2026-01-21")
        assert result["valid"] is False
        # Either rejected as missing timezone or parsed as naive date

    def test_unix_timestamp_string(self):
        result = validate_round_id("1737475200")
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# Flag building and whitelist enforcement
# ---------------------------------------------------------------------------

class TestValidateAndBuildFlags:
    """~23 tests covering every tool, type check, range, enum, bool, and error paths."""

    # -- Unknown / empty --

    def test_unknown_tool_name(self):
        result = validate_and_build_flags("samtools", {"foo": 1})
        assert result["valid"] is False
        assert any("Unknown tool" in e for e in result["errors"])
        assert result["flags"] == []

    def test_empty_options_gatk(self):
        result = validate_and_build_flags("gatk", {})
        assert result["valid"] is True
        assert result["flags"] == []
        assert result["errors"] == []

    def test_unknown_param_in_gatk(self):
        result = validate_and_build_flags("gatk", {"threads": 8})
        assert result["valid"] is False
        assert any("not in quality params whitelist" in e for e in result["errors"])

    # -- GATK: int param --

    def test_gatk_valid_int(self):
        result = validate_and_build_flags("gatk", {"min_base_quality_score": 20})
        assert result["valid"] is True
        assert "--min-base-quality-score 20" in result["flags"]

    def test_gatk_int_out_of_range_low(self):
        result = validate_and_build_flags("gatk", {"min_pruning": 0})
        assert result["valid"] is False
        assert any("out of range" in e for e in result["errors"])

    def test_gatk_int_out_of_range_high(self):
        result = validate_and_build_flags("gatk", {"min_pruning": 99})
        assert result["valid"] is False
        assert any("out of range" in e for e in result["errors"])

    # -- GATK: float param --

    def test_gatk_valid_float(self):
        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": 50.0}
        )
        assert result["valid"] is True
        assert "--standard-min-confidence-threshold-for-calling 50.0" in result["flags"]

    def test_gatk_float_accepts_int_value(self):
        """Float params should accept plain int values (int is a subtype of float)."""
        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": 50}
        )
        assert result["valid"] is True

    def test_gatk_float_wrong_type(self):
        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": "high"}
        )
        assert result["valid"] is False
        assert any("must be float" in e for e in result["errors"])

    # -- GATK: enum param --

    def test_gatk_valid_enum(self):
        result = validate_and_build_flags("gatk", {"emit_ref_confidence": "GVCF"})
        assert result["valid"] is True
        assert "--emit-ref-confidence GVCF" in result["flags"]

    def test_gatk_invalid_enum(self):
        result = validate_and_build_flags("gatk", {"emit_ref_confidence": "INVALID"})
        assert result["valid"] is False
        assert any("not in allowed values" in e for e in result["errors"])

    # -- GATK: bool param --

    def test_gatk_bool_true_emits_flag(self):
        result = validate_and_build_flags(
            "gatk", {"recover_all_dangling_branches": True}
        )
        assert result["valid"] is True
        assert "--recover-all-dangling-branches" in result["flags"]

    def test_gatk_bool_false_no_flag(self):
        result = validate_and_build_flags(
            "gatk", {"recover_all_dangling_branches": False}
        )
        assert result["valid"] is True
        assert result["flags"] == []

    def test_gatk_bool_wrong_type(self):
        result = validate_and_build_flags(
            "gatk", {"recover_all_dangling_branches": 1}
        )
        assert result["valid"] is False
        assert any("must be bool" in e for e in result["errors"])

    # -- DeepVariant --

    def test_deepvariant_model_type_enum(self):
        result = validate_and_build_flags("deepvariant", {"model_type": "WGS"})
        assert result["valid"] is True
        assert "--model_type WGS" in result["flags"]

    def test_deepvariant_make_examples_stage(self):
        result = validate_and_build_flags(
            "deepvariant", {"vsc_min_fraction_snps": 0.15}
        )
        assert result["valid"] is True
        assert len(result["flags"]) == 1
        flag = result["flags"][0]
        assert flag["stage"] == "make_examples"
        assert flag["param"] == "vsc_min_fraction_snps=0.15"

    def test_deepvariant_postprocess_variants_stage(self):
        result = validate_and_build_flags("deepvariant", {"qual_filter": 5.0})
        assert result["valid"] is True
        flag = result["flags"][0]
        assert flag["stage"] == "postprocess_variants"
        assert flag["param"] == "qual_filter=5.0"

    def test_deepvariant_bool_stage_param(self):
        """Bool params in make_examples are lowered to 'true'/'false' strings."""
        result = validate_and_build_flags("deepvariant", {"realign_reads": True})
        assert result["valid"] is True
        flag = result["flags"][0]
        assert flag["stage"] == "make_examples"
        assert flag["param"] == "realign_reads=true"

    # -- FreeBayes --

    def test_freebayes_valid_multi_param(self):
        opts = {
            "min_mapping_quality": 20,
            "min_alternate_fraction": 0.1,
            "ploidy": 2,
        }
        result = validate_and_build_flags("freebayes", opts)
        assert result["valid"] is True
        assert len(result["flags"]) == 3
        assert "--min-mapping-quality 20" in result["flags"]
        assert "--min-alternate-fraction 0.1" in result["flags"]
        assert "--ploidy 2" in result["flags"]

    # -- BCFtools --

    def test_bcftools_mpileup_stage_flag(self):
        result = validate_and_build_flags("bcftools", {"min_MQ": 30})
        assert result["valid"] is True
        flag = result["flags"][0]
        assert flag["stage"] == "mpileup"
        assert flag["flag"] == "-q 30"

    def test_bcftools_call_stage_flag(self):
        result = validate_and_build_flags("bcftools", {"ploidy": "GRCh38"})
        assert result["valid"] is True
        flag = result["flags"][0]
        assert flag["stage"] == "call"
        assert flag["flag"] == "--ploidy GRCh38"

    def test_bcftools_bool_flag_true(self):
        result = validate_and_build_flags("bcftools", {"no_BAQ": True})
        assert result["valid"] is True
        flag = result["flags"][0]
        assert flag["stage"] == "mpileup"
        assert flag["flag"] == "-B"

    def test_bcftools_bool_flag_false(self):
        result = validate_and_build_flags("bcftools", {"no_BAQ": False})
        assert result["valid"] is True
        assert result["flags"] == []

    # -- Mixed valid + invalid --

    def test_mixed_valid_and_invalid_params(self):
        opts = {
            "min_base_quality_score": 20,   # valid
            "threads": 8,                    # unknown
            "min_pruning": 5,               # valid
        }
        result = validate_and_build_flags("gatk", opts)
        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert any("threads" in e for e in result["errors"])
        # The valid params still produce flags
        assert "--min-base-quality-score 20" in result["flags"]
        assert "--min-pruning 5" in result["flags"]
