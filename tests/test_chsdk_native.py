"""Native installation must never need the optional CHSDK."""
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("setting", [None, "native"])
def test_native_hooks_without_chsdk(setting):
    code = textwrap.dedent('''
        import builtins
        import subprocess
        import sys
        from unittest.mock import patch

        original = builtins.__import__
        def blocked(name, *args, **kwargs):
            if name.startswith("compute_horde_sdk") or name.endswith("_chsdk"):
                raise AssertionError("native mode imported the CHSDK")
            return original(name, *args, **kwargs)
        builtins.__import__ = blocked
        from templates import _common
        cmd = ["docker", "run", "--rm", "--name", "native-test", "image"]
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(cmd, 0, "native-test\\n", "")) as run:
            _common.run_container(cmd, capture_output=True, text=True)
            run.assert_called_once_with(cmd, capture_output=True, text=True)
            _common.begin_round()
            _common.run_container(cmd, capture_output=True, text=True)
            assert not _common._live_containers
            _common.reap_container("native-test")
            _common._live_containers.add("native-test")
            assert _common.reap_live_containers() == 1
            assert run.call_args.kwargs["timeout"] == 20
        assert "templates._chsdk" not in sys.modules
        assert not any(k.startswith("compute_horde_sdk") for k in sys.modules)
    ''')
    env = os.environ.copy()
    env.pop("CONTAINER_RUNNER", None)
    if setting is not None:
        env["CONTAINER_RUNNER"] = setting
    subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True)


def test_unknown_runner_is_an_error(monkeypatch):
    from templates import _common
    monkeypatch.setenv("CONTAINER_RUNNER", "typo")
    with pytest.raises(ValueError, match="CONTAINER_RUNNER"):
        _common.run_container([])
