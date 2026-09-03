"""Tests for neurons/status.py — the operator health command.

The failure mode that matters is a wrong diagnosis: reporting a healthy node as
broken, or not returning at all. The checks here assert on the observed
behaviour of the diagnosis, not on wording.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from neurons import status

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_role_env(monkeypatch):
    """Neither MINER_TEMPLATE nor MINER_DEMO leaking in from a dev .env."""
    monkeypatch.delenv("MINER_TEMPLATE", raising=False)
    monkeypatch.delenv("MINER_DEMO", raising=False)


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------

def test_unset_miner_template_is_a_default_miner(tmp_path):
    """An unset MINER_TEMPLATE means DEFAULT_TEMPLATE, not "not a miner"."""
    from templates import DEFAULT_TEMPLATE

    role, template = status.detect_role(base_dir=tmp_path)

    assert role == "miner"
    assert template == DEFAULT_TEMPLATE


def test_default_miner_is_not_diagnosed_against_validator_assets(tmp_path):
    """A default miner must be diagnosed as a miner.

    Diagnosed as a validator it would be checked against hap.py/DeepVariant/
    freebayes images and 22 .sdf directories it does not own, and the gatk.conf
    parse — the check that actually matters for it — would be skipped.
    """
    role, template = status.detect_role(base_dir=tmp_path)

    ref_checks = status.check_reference_files(role)
    assert not [c for c in ref_checks if ".sdf" in c.name]

    config_check = status.check_config_files(template)
    assert config_check.status is status.Status.PASS
    assert "gatk.conf" in config_check.detail


def test_explicit_miner_template_is_a_miner(tmp_path, monkeypatch):
    monkeypatch.setenv("MINER_TEMPLATE", " BCFTools ")
    assert status.detect_role(base_dir=tmp_path) == ("miner", "bcftools")


def test_env_validator_file_detects_validator(tmp_path):
    (tmp_path / ".env.validator").write_text("NETUID=107\n")

    role, template = status.detect_role(base_dir=tmp_path)

    assert role == "validator"
    assert template is None
    assert [c for c in status.check_reference_files(role) if ".sdf" in c.name]


def test_miner_template_beats_a_stray_env_validator_file(tmp_path, monkeypatch):
    """setup.py writes MINER_TEMPLATE only for miners, so it is miner evidence."""
    (tmp_path / ".env.validator").write_text("NETUID=107\n")
    monkeypatch.setenv("MINER_TEMPLATE", "gatk")

    assert status.detect_role(base_dir=tmp_path) == ("miner", "gatk")


def test_both_env_files_present_falls_back_to_miner(tmp_path):
    (tmp_path / ".env.validator").write_text("NETUID=107\n")
    (tmp_path / ".env.miner").write_text("NETUID=107\n")

    role, _ = status.detect_role(base_dir=tmp_path)

    assert role == "miner"


def test_explicit_role_overrides_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("MINER_TEMPLATE", "gatk")
    role, template = status.detect_role("validator", base_dir=tmp_path)
    assert role == "validator"
    assert template == "gatk"

    (tmp_path / ".env.validator").write_text("NETUID=107\n")
    monkeypatch.delenv("MINER_TEMPLATE")
    assert status.detect_role("miner", base_dir=tmp_path) == ("miner", "gatk")


# ---------------------------------------------------------------------------
# Reference layout (per-chromosome only; demo installs chr20 alone)
# ---------------------------------------------------------------------------

def test_reference_paths_are_per_chromosome_only():
    """setup.py migrates the flat layout away, so no flat path is expected."""
    paths = status._build_reference_files(["chr20"]) + status._build_reference_dirs(["chr20"])

    assert paths == [
        "datasets/reference/chr20/chr20.fa",
        "datasets/reference/chr20/chr20.fa.fai",
        "datasets/reference/chr20/chr20.dict",
        "datasets/reference/chr20/chr20.sdf",
    ]
    assert "flat" not in (status.check_reference_files.__doc__ or "")


def test_demo_mode_only_expects_chr20(monkeypatch):
    """setup.py restricts the download to /chr20/ when MINER_DEMO is truthy."""
    assert status.expected_chromosomes() == status.SUPPORTED_CHROMOSOMES

    monkeypatch.setenv("MINER_DEMO", "True")
    assert status.expected_chromosomes() == ["chr20"]
    assert len(status.check_reference_files("miner")) == 3

    monkeypatch.setenv("MINER_DEMO", "false")
    assert status.expected_chromosomes() == status.SUPPORTED_CHROMOSOMES
    assert len(status.check_reference_files("miner")) == 66


# ---------------------------------------------------------------------------
# Chain probe timeout
# ---------------------------------------------------------------------------

@pytest.fixture
def blocker():
    """A callable that blocks until released, with the release guaranteed."""
    released = threading.Event()

    def block():
        released.wait(30)
        return "finished"

    yield block
    released.set()


def test_call_with_timeout_returns_value_and_reraises():
    assert status._call_with_timeout(lambda: "ok", 5) == "ok"

    def boom():
        raise ImportError("bittensor not installed")

    with pytest.raises(ImportError):
        status._call_with_timeout(boom, 5)


def test_call_with_timeout_fires_while_the_worker_is_still_running(blocker):
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        status._call_with_timeout(blocker, 0.2)
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"timeout waited for the worker ({elapsed:.1f}s)"


def test_probe_thread_is_daemon(blocker):
    with pytest.raises(TimeoutError):
        status._call_with_timeout(blocker, 0.1)

    probes = [t for t in threading.enumerate() if t.name == "minos-status-probe"]
    assert probes, "probe thread already gone; test cannot check the flag"
    assert all(t.daemon for t in probes), "non-daemon probe blocks interpreter exit"


def test_chain_check_reports_timeout_against_a_black_holed_endpoint(monkeypatch, blocker):
    """A subtensor that never answers must yield a WARN, not a hang."""
    import types

    fake = types.ModuleType("bittensor")
    fake.subtensor = lambda network=None: blocker()
    monkeypatch.setitem(sys.modules, "bittensor", fake)

    start = time.monotonic()
    check = status.check_bittensor_chain(timeout=0.3)
    elapsed = time.monotonic() - start

    assert check.status is status.Status.WARN
    assert "timeout" in check.detail
    assert elapsed < 5, f"check_bittensor_chain blocked for {elapsed:.1f}s"


def test_hung_probe_does_not_keep_the_process_alive():
    """An abandoned probe must not delay interpreter exit, so `status.py
    --json` returns and exits even when the chain probe never answers.
    """
    program = (
        "import threading, neurons.status as s\n"
        "e = threading.Event()\n"
        "try:\n"
        "    s._call_with_timeout(lambda: e.wait(120), 0.2)\n"
        "except TimeoutError:\n"
        "    print('TIMED_OUT', flush=True)\n"
    )
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    elapsed = time.monotonic() - start

    assert "TIMED_OUT" in proc.stdout
    assert proc.returncode == 0
    assert elapsed < 20, f"process took {elapsed:.1f}s to exit with a probe running"


# ---------------------------------------------------------------------------
# Role detection from the setup-generated .env header
# ---------------------------------------------------------------------------

SETUP_ENV = """# Minos {label} Configuration
# Generated by setup wizard on 2026-08-30 10:00:00

# Bittensor
NETUID=107
"""


@pytest.mark.parametrize("label,expected", [
    ("Validator", "validator"),
    ("Miner", "miner"),
    ("Miner (demo)", "miner"),
])
def test_role_from_generated_env_header(tmp_path, label, expected):
    """setup.py writes a plain .env for both roles; the header is the only
    positive evidence a validator install leaves behind."""
    (tmp_path / ".env").write_text(SETUP_ENV.format(label=label))

    role, _ = status.detect_role(base_dir=tmp_path)

    assert role == expected


def test_headerless_env_falls_back_to_miner(tmp_path):
    (tmp_path / ".env").write_text("NETUID=107\nWALLET_NAME=default\n")

    assert status.detect_role(base_dir=tmp_path) == ("miner", "gatk")
