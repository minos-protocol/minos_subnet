"""Tests for utils.path_utils.safe_round_dir_name."""

import re

from utils.path_utils import safe_round_dir_name


def test_known_input_produces_expected_output():
    """SHA-256 of '2026-01-21T12:00:00+00:00' truncated to 16 hex chars."""
    result = safe_round_dir_name("2026-01-21T12:00:00+00:00")
    assert result == "round_c88e6e2e6b1e3c23"


def test_different_round_ids_produce_different_names():
    a = safe_round_dir_name("2026-01-21T12:00:00+00:00")
    b = safe_round_dir_name("2026-01-22T12:00:00+00:00")
    assert a != b


def test_format_matches_expected_regex():
    result = safe_round_dir_name("any-round-id")
    assert re.fullmatch(r"round_[0-9a-f]{16}", result)


def test_adversarial_input_produces_safe_name():
    result = safe_round_dir_name("../../etc/passwd")
    assert "/" not in result
    assert ".." not in result
    assert re.fullmatch(r"round_[0-9a-f]{16}", result)


def test_empty_string_is_deterministic():
    result = safe_round_dir_name("")
    # SHA-256 of empty string is e3b0c44298fc1c14...
    assert result == "round_e3b0c44298fc1c14"
