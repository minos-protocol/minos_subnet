"""Tests for config parsing and parameter validation.

Three properties the validation layer has to hold, each easy to get wrong:

  * NaN must be rejected by the numeric range checks. ``nan < min`` and
    ``nan > max`` are both False, so a plain range test lets it through and it
    reaches the command line as ``nan``.
  * ``bool`` is a subclass of ``int``, so an int parameter must reject ``True``
    rather than rendering it as the literal ``True`` in the flag.
  * .conf files are read as UTF-8 with BOM handling, so a file saved by a
    Windows editor decodes and its first key name stays intact.
"""

import logging
import math

import pytest

from templates.tool_params import (
    BCFTOOLS_QUALITY_PARAMS,
    GATK_QUALITY_PARAMS,
    validate_and_build_flags,
)
from utils.config_loader import extract_tool_options


def _flag_text(flags):
    """Flatten the flag list (bcftools yields dicts, others plain strings)."""
    out = []
    for f in flags:
        if isinstance(f, dict):
            out.append(f.get("flag") or f.get("param") or "")
        else:
            out.append(str(f))
    return out


# ---------------------------------------------------------------------------
# Non-finite floats
# ---------------------------------------------------------------------------


class TestNonFiniteRejected:

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_float_param_rejects_non_finite(self, value):
        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": value}
        )
        assert result["valid"] is False
        assert result["flags"] == []
        assert result["errors"]

    def test_nan_never_reaches_the_command_line(self):
        """nan compares False against both bounds, so a plain range check
        lets it through."""
        nan = float("nan")
        assert not (nan < 0) and not (nan > 100), "premise: nan loses both comparisons"

        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": nan}
        )
        assert not any("nan" in f.lower() for f in _flag_text(result["flags"]))

    def test_every_float_param_rejects_nan(self):
        """No float parameter in any tool may accept a non-finite value."""
        for tool, params in (
            ("gatk", GATK_QUALITY_PARAMS),
            ("bcftools", BCFTOOLS_QUALITY_PARAMS),
        ):
            for name, spec in params.items():
                if spec["type"] != "float":
                    continue
                result = validate_and_build_flags(tool, {name: float("nan")})
                assert result["valid"] is False, f"{tool}.{name} accepted nan"

    def test_finite_float_still_accepted(self):
        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": 30.0}
        )
        assert result["valid"] is True, result["errors"]
        assert _flag_text(result["flags"]) == [
            f"{GATK_QUALITY_PARAMS['standard_min_confidence_threshold_for_calling']['flag']} 30.0"
        ]

    def test_int_valued_float_param_still_accepted(self):
        """The field submits 30 as well as 30.0 for the same parameter."""
        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": 30}
        )
        assert result["valid"] is True, result["errors"]


# ---------------------------------------------------------------------------
# bool where a number is expected
# ---------------------------------------------------------------------------


class TestBoolRejectedForNumericParams:

    def test_isinstance_premise(self):
        assert isinstance(True, int), "premise: bool is a subclass of int"

    @pytest.mark.parametrize("value", [True, False])
    def test_int_param_rejects_bool(self, value):
        result = validate_and_build_flags("gatk", {"min_pruning": value})
        assert result["valid"] is False
        assert result["flags"] == []
        assert any("must be int" in e for e in result["errors"])

    @pytest.mark.parametrize("value", [True, False])
    def test_float_param_rejects_bool(self, value):
        result = validate_and_build_flags(
            "gatk", {"standard_min_confidence_threshold_for_calling": value}
        )
        assert result["valid"] is False
        assert any("must be float" in e for e in result["errors"])

    def test_bool_never_renders_as_a_literal_in_a_flag(self):
        """Without an explicit bool check, True satisfies the int check and
        renders as '--min-pruning True'."""
        result = validate_and_build_flags("gatk", {"min_pruning": True})
        assert not any("True" in f for f in _flag_text(result["flags"]))

    def test_no_int_or_float_param_accepts_a_bool(self):
        for tool, params in (
            ("gatk", GATK_QUALITY_PARAMS),
            ("bcftools", BCFTOOLS_QUALITY_PARAMS),
        ):
            for name, spec in params.items():
                if spec["type"] not in ("int", "float"):
                    continue
                result = validate_and_build_flags(tool, {name: True})
                assert result["valid"] is False, f"{tool}.{name} accepted True"

    def test_genuine_bool_param_still_works(self):
        on = validate_and_build_flags("bcftools", {"no_BAQ": True})
        assert on["valid"] is True, on["errors"]
        assert len(on["flags"]) == 1

        off = validate_and_build_flags("bcftools", {"no_BAQ": False})
        assert off["valid"] is True, off["errors"]
        assert off["flags"] == []

    def test_genuine_bool_param_rejects_an_int(self):
        result = validate_and_build_flags("bcftools", {"no_BAQ": 1})
        assert result["valid"] is False

    def test_ordinary_int_param_still_works(self):
        result = validate_and_build_flags("gatk", {"min_pruning": 3})
        assert result["valid"] is True, result["errors"]
        assert _flag_text(result["flags"]) == [
            f"{GATK_QUALITY_PARAMS['min_pruning']['flag']} 3"
        ]

    def test_bcftools_ploidy_int_preset_still_normalises(self):
        """Guarding bool must not break the int -> enum-string normalisation."""
        result = validate_and_build_flags("bcftools", {"ploidy": 2})
        assert result["valid"] is True, result["errors"]
        assert any("2" in f for f in _flag_text(result["flags"]))

    def test_deepvariant_bool_still_lowercased(self):
        result = validate_and_build_flags("deepvariant", {"realign_reads": True})
        assert result["valid"] is True, result["errors"]
        assert any("true" in f for f in _flag_text(result["flags"]))


# ---------------------------------------------------------------------------
# Whitelist rejection: contract and error message
# ---------------------------------------------------------------------------


class TestUnknownKeyHandling:

    def test_unknown_key_still_voids_the_whole_config(self):
        """Strictness is deliberately unchanged: this is the enforcement point."""
        result = validate_and_build_flags(
            "gatk", {"min_pruning": 3, "not_a_real_param": 1}
        )
        # Flags for the good keys are still built, but every template gates on
        # "valid" and aborts the run, so the whole submission is lost.
        assert result["valid"] is False

    def test_error_says_the_whole_config_is_rejected(self):
        result = validate_and_build_flags("gatk", {"not_a_real_param": 1})
        (msg,) = result["errors"]
        assert "not_a_real_param" in msg
        assert "not in quality params whitelist" in msg  # relied on elsewhere
        assert "ENTIRE" in msg

    def test_all_unknown_keys_are_reported_not_just_the_first(self):
        result = validate_and_build_flags("gatk", {"bogus_a": 1, "bogus_b": 2})
        joined = " ".join(result["errors"])
        assert "bogus_a" in joined and "bogus_b" in joined


# ---------------------------------------------------------------------------
# .conf loading: encoding, BOM, and the load-time whitelist warning
# ---------------------------------------------------------------------------


class TestConfEncoding:

    def _write(self, tmp_path, tool, text, encoding):
        (tmp_path / f"{tool}.conf").write_text(text, encoding=encoding)

    def test_bom_is_stripped_from_the_first_key(self, tmp_path, monkeypatch):
        """A Windows editor's BOM must not become part of the first key name."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write(
            tmp_path, "gatk",
            "min_base_quality_score=10\nmin_pruning=3\n",
            "utf-8-sig",
        )

        options = extract_tool_options("gatk")

        assert "min_base_quality_score" in options
        assert not any(k.startswith("﻿") for k in options)
        assert validate_and_build_flags("gatk", options)["valid"] is True

    def test_a_bom_config_still_validates(self, tmp_path, monkeypatch):
        """End to end: a BOM must not reach the parameter names, so a config
        saved by a Windows editor validates like any other."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write(tmp_path, "gatk", "min_pruning=3\n", "utf-8-sig")

        assert validate_and_build_flags(
            "gatk", extract_tool_options("gatk")
        )["valid"] is True

    def test_utf8_values_decode_regardless_of_process_locale(self, tmp_path, monkeypatch):
        """Non-ASCII bytes raise UnicodeDecodeError under a C locale, so the
        read pins UTF-8 rather than following the process locale."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write(tmp_path, "gatk", "# café naïve\nmin_pruning=3\n", "utf-8")

        assert extract_tool_options("gatk")["min_pruning"] == 3

    def test_plain_utf8_file_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write(tmp_path, "gatk", "min_pruning=3\npcr_indel_model=NONE\n", "utf-8")

        options = extract_tool_options("gatk")
        assert options == {"min_pruning": 3, "pcr_indel_model": "NONE"}

    def test_crlf_line_endings_parse(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        (tmp_path / "gatk.conf").write_bytes(b"\xef\xbb\xbfmin_pruning=3\r\n")

        assert extract_tool_options("gatk") == {"min_pruning": 3}


class TestLoadTimeWhitelistWarning:

    def test_unknown_key_warns_but_is_still_returned(self, tmp_path, monkeypatch, caplog):
        """Warn while the miner can still edit the file; do not drop the key."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        (tmp_path / "gatk.conf").write_text("min_pruning=3\nmin_prunning=4\n")

        with caplog.at_level(logging.WARNING, logger="utils.config_loader"):
            options = extract_tool_options("gatk")

        assert options["min_prunning"] == 4, "strictness contract is unchanged"
        assert "min_prunning" in caplog.text
        assert "min_pruning=" not in caplog.text  # the valid key is not named

    def test_clean_config_warns_about_nothing(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        (tmp_path / "gatk.conf").write_text("min_pruning=3\n")

        with caplog.at_level(logging.WARNING, logger="utils.config_loader"):
            extract_tool_options("gatk")

        assert caplog.text == ""

    def test_shipped_configs_are_clean(self):
        """Every .conf in configs/ must validate as shipped."""
        for tool in ("gatk", "deepvariant", "bcftools"):
            options = extract_tool_options(tool)
            result = validate_and_build_flags(tool, options)
            assert result["valid"] is True, (tool, result["errors"])


# ---------------------------------------------------------------------------
# End to end: a crafted .conf cannot smuggle nan/True through the loader
# ---------------------------------------------------------------------------


def test_conf_file_cannot_smuggle_non_finite_or_bool(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
    (tmp_path / "gatk.conf").write_text(
        "standard_min_confidence_threshold_for_calling=nan\nmin_pruning=true\n"
    )

    options = extract_tool_options("gatk")
    # _parse_value() routes "nan" through float() and "true" through the bool
    # branch, which is how both reached validation in the first place.
    assert math.isnan(options["standard_min_confidence_threshold_for_calling"])
    assert options["min_pruning"] is True

    result = validate_and_build_flags("gatk", options)
    assert result["valid"] is False
    assert len(result["errors"]) == 2
    assert result["flags"] == []
