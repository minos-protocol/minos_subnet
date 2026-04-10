"""Tests for utils.config_loader module."""

import pytest

from utils.config_loader import _parse_value, extract_tool_options, get_tool_version, CONFIG_DIR


# ---------------------------------------------------------------------------
# TestParseValue
# ---------------------------------------------------------------------------


class TestParseValue:
    """Tests for the _parse_value helper."""

    def test_true_lowercase(self):
        assert _parse_value("true") is True

    def test_false_lowercase(self):
        assert _parse_value("false") is False

    def test_true_mixed_case(self):
        assert _parse_value("True") is True

    def test_integer(self):
        result = _parse_value("42")
        assert result == 42
        assert isinstance(result, int)

    def test_float(self):
        result = _parse_value("3.14")
        assert result == 3.14
        assert isinstance(result, float)

    def test_string_passthrough(self):
        assert _parse_value("CONSERVATIVE") == "CONSERVATIVE"

    def test_whitespace_stripped(self):
        result = _parse_value(" 42 ")
        assert result == 42
        assert isinstance(result, int)

    def test_small_float_not_int(self):
        result = _parse_value("0.001")
        assert result == 0.001
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# TestExtractToolOptions
# ---------------------------------------------------------------------------


class TestExtractToolOptions:
    """Tests for extract_tool_options, using tmp_path + monkeypatch."""

    def _write_conf(self, tmp_path, tool, content):
        """Helper: write a .conf file into tmp_path and return its path."""
        conf = tmp_path / f"{tool}.conf"
        conf.write_text(content)
        return conf

    def test_valid_config(self, tmp_path, monkeypatch):
        """Correct types and values are returned for a well-formed config."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write_conf(
            tmp_path,
            "gatk",
            "min_base_quality_score=10\npcr_indel_model=CONSERVATIVE\nenable_flag=true\nthreshold=0.05\n",
        )

        result = extract_tool_options("gatk")

        assert result["min_base_quality_score"] == 10
        assert isinstance(result["min_base_quality_score"], int)
        assert result["pcr_indel_model"] == "CONSERVATIVE"
        assert result["enable_flag"] is True
        assert result["threshold"] == pytest.approx(0.05)
        assert isinstance(result["threshold"], float)

    def test_empty_file(self, tmp_path, monkeypatch):
        """An empty config file produces an empty dict."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write_conf(tmp_path, "empty", "")

        assert extract_tool_options("empty") == {}

    def test_comments_only(self, tmp_path, monkeypatch):
        """A file with only comments and blank lines produces an empty dict."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write_conf(tmp_path, "commented", "# This is a comment\n\n# Another comment\n")

        assert extract_tool_options("commented") == {}

    def test_missing_equals_raises(self, tmp_path, monkeypatch):
        """A line without '=' raises ValueError."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write_conf(tmp_path, "bad", "no_equals_here\n")

        with pytest.raises(ValueError, match="missing '='"):
            extract_tool_options("bad")

    def test_empty_key_raises(self, tmp_path, monkeypatch):
        """A line like '=value' (empty key) raises ValueError."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write_conf(tmp_path, "badkey", "=some_value\n")

        with pytest.raises(ValueError, match="Empty key"):
            extract_tool_options("badkey")

    def test_file_not_found(self, tmp_path, monkeypatch):
        """Requesting a tool with no config file raises FileNotFoundError."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)

        with pytest.raises(FileNotFoundError, match="Config file not found"):
            extract_tool_options("nonexistent")

    def test_spaces_around_equals(self, tmp_path, monkeypatch):
        """Spaces around '=' are handled correctly."""
        monkeypatch.setattr("utils.config_loader.CONFIG_DIR", tmp_path)
        self._write_conf(tmp_path, "spaced", "key_one = 100\nkey_two =hello\nkey_three= false\n")

        result = extract_tool_options("spaced")

        assert result["key_one"] == 100
        assert isinstance(result["key_one"], int)
        assert result["key_two"] == "hello"
        assert result["key_three"] is False


# ---------------------------------------------------------------------------
# TestGetToolVersion
# ---------------------------------------------------------------------------


class TestGetToolVersion:
    """Tests for get_tool_version."""

    def test_known_tool(self):
        assert get_tool_version("gatk") == "4.5.0.0"

    def test_unknown_tool_returns_default(self):
        assert get_tool_version("unknown") == "1.0.0"
