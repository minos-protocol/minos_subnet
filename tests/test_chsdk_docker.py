"""Opt-in disposable Linux Docker tests. Never mounts validator data."""
import contextvars
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

pytest.importorskip("compute_horde_sdk")
from compute_horde_sdk.v1 import local as chsdk
from templates import _common

pytestmark = pytest.mark.skipif(
    os.getenv("MINOS_CHSDK_DOCKER_TESTS") != "1", reason="Opt-in disposable Docker tests",
)
IMAGE = "busybox:1.37"


@pytest.fixture(autouse=True)
def chsdk_runner(monkeypatch):
    monkeypatch.setenv("CONTAINER_RUNNER", "chsdk")
    token = _common._job_round.set(None)
    _common.begin_round()
    try:
        yield
    finally:
        _common.reap_live_containers()
        _common._job_round.reset(token)


def cmd(tmp_path, script, name=None):
    return ["docker", "run", "--rm", "--name", name or _common.container_name("chsdk-test"),
            "--cpus=0.5", "--memory=64m", "-u", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{tmp_path}:/work", IMAGE, "sh", "-ec", script]


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Disposable container did not reach expected state")
        time.sleep(0.01)


def test_direct_binds_producer_consumer_and_parallel_outputs(tmp_path):
    reference = tmp_path / "reference.fa"
    reference.write_text(">ref\nACGT\n")
    producer = cmd(tmp_path, "cat /ref.fa > /work/produced; "
                            "if touch /readonly/forbidden 2>/dev/null; then exit 88; fi")
    image_at = producer.index(IMAGE)
    producer[image_at:image_at] = ["-v", f"{reference}:/ref.fa:ro", "-v", f"{tmp_path}:/readonly:ro"]
    result = _common.run_container(producer, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "produced").read_bytes() == reference.read_bytes()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(contextvars.copy_context().run, _common.run_container,
                               cmd(tmp_path, f"cat /work/produced > /work/miner-{i}"),
                               capture_output=True, timeout=15) for i in range(3)]
        assert [f.result().returncode for f in futures] == [0, 0, 0]
    assert [(tmp_path / f"miner-{i}").read_bytes() for i in range(3)] == [reference.read_bytes()] * 3
    assert not _common._live_containers


def test_stdout_file_streams_completely_and_separates_stderr(tmp_path):
    output = tmp_path / "stdout.vcf"
    script = "head -c 3000000 /dev/zero; echo ready > /work/ready; sleep 2; printf tail; printf diagnostic >&2"
    with output.open("w") as sink, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(contextvars.copy_context().run, _common.run_container, cmd(tmp_path, script),
                             stdout=sink, stderr=subprocess.PIPE, text=True, timeout=30)
        wait_for(lambda: (tmp_path / "ready").exists())
        wait_for(lambda: output.stat().st_size > 1000000)
        assert not future.done()
        result = future.result(15)
        assert not sink.closed
        assert output.read_bytes() == b"\0" * 3000000 + b"tail"
    assert (result.returncode, result.stdout, result.stderr) == (0, None, "diagnostic")


def test_single_force_stop_and_timeout(tmp_path):
    name = _common.container_name("chsdk-stop")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(contextvars.copy_context().run, _common.run_container,
                             cmd(tmp_path, "echo ready > /work/ready; sleep 60", name),
                             capture_output=True, timeout=30)
        wait_for(lambda: (tmp_path / "ready").exists())
        _common.reap_container(name)
        _common.reap_container(name)
        with pytest.raises(chsdk.LocalJobCancelledError):
            future.result(5)
    assert subprocess.run(["docker", "container", "inspect", name], capture_output=True).returncode != 0
    with pytest.raises(subprocess.TimeoutExpired):
        _common.run_container(cmd(tmp_path, "sleep 60"), capture_output=True, timeout=0.3)
    assert not _common._live_containers


def test_round_stops_parallel_jobs(tmp_path):
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(contextvars.copy_context().run, _common.run_container,
                               cmd(tmp_path, f"echo ready > /work/ready-{i}; sleep 60"),
                               capture_output=True, timeout=30) for i in range(3)]
        wait_for(lambda: all((tmp_path / f"ready-{i}").exists() for i in range(3)))
        assert _common.reap_live_containers(3) == 3
        for future in futures:
            with pytest.raises(chsdk.LocalJobCancelledError):
                future.result(5)
    assert _common.reap_live_containers() == 0
    with pytest.raises(_common.RegistryClosed):
        _common.run_container(cmd(tmp_path, "true"), capture_output=True)


def test_image_entrypoint_and_exact_arguments(tmp_path):
    image = f"minos-chsdk-test-{uuid4().hex}:latest"
    (tmp_path / "Dockerfile").write_text('FROM busybox:1.37\nENTRYPOINT ["printf", "%s\\n"]\n')
    build_env = {**os.environ, "BUILDX_CONFIG": str(tmp_path / "buildx")}
    built = subprocess.run(["docker", "build", "-t", image, str(tmp_path)], env=build_env,
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    try:
        args = ["a b", "$(printf INJECTION)", "", '"quoted"']
        command = ["docker", "run", "--rm", "--name", _common.container_name("chsdk-entrypoint"), image, *args]
        result = _common.run_container(command, capture_output=True, text=True, timeout=15)
        assert (result.returncode, result.stdout, result.stderr) == (0, "\n".join(args) + "\n", "")
    finally:
        subprocess.run(["docker", "image", "rm", image], check=True, capture_output=True)


def test_logging_matches_native_daemon_defaults(tmp_path, monkeypatch):
    names = {runner: _common.container_name(f"chsdk-logs-{runner}") for runner in ("native", "chsdk")}
    workdirs = {runner: tmp_path / runner for runner in names}
    for directory in workdirs.values():
        directory.mkdir()

    def inspect(name):
        result = subprocess.run(["docker", "container", "inspect", name],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            assert "No such" in result.stderr, result.stderr
            return None
        return json.loads(result.stdout)[0]

    def logs(name):
        return subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=5)

    futures = {}
    unreadable = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            for runner, name in names.items():
                monkeypatch.setenv("CONTAINER_RUNNER", runner)
                script = (
                    f"printf '{runner}-stdout\\n'; printf '{runner}-stderr\\n' >&2; "
                    "touch /work/ready; "
                    "while [ ! -e /work/release ]; do sleep 0.05; done; exit 7"
                )
                futures[runner] = pool.submit(
                    contextvars.copy_context().run, _common.run_container,
                    cmd(workdirs[runner], script, name), capture_output=True, text=True, timeout=60,
                )
                # Each runner has entered execution before the setting changes.
                wait_for(lambda: (workdirs[runner] / "ready").exists())

            info = {runner: inspect(name) for runner, name in names.items()}
            assert all(container["State"]["Running"] for container in info.values())
            native_config = info["native"]["HostConfig"]["LogConfig"]
            assert info["chsdk"]["HostConfig"]["LogConfig"] == native_config
            daemon_driver = subprocess.run(
                ["docker", "info", "--format", "{{.LoggingDriver}}"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            assert native_config["Type"] == daemon_driver

            native_logs = logs(names["native"])
            if native_logs.returncode:
                assert "does not support reading" in native_logs.stderr, native_logs.stderr
                unreadable = (
                    f"Native docker logs is unreadable with daemon driver {daemon_driver!r}: "
                    f"{native_logs.stderr.strip()}; log-read parity requires a readable daemon driver"
                )
            else:
                for runner, name in names.items():
                    def markers_logged():
                        result = logs(name)
                        assert result.returncode == 0, result.stderr
                        return (f"{runner}-stdout\n" in result.stdout
                                and f"{runner}-stderr\n" in result.stderr)
                    wait_for(markers_logged)

            for directory in workdirs.values():
                (directory / "release").touch()
            for runner, future in futures.items():
                result = future.result(15)
                assert (result.returncode, result.stdout, result.stderr) == (
                    7, f"{runner}-stdout\n", f"{runner}-stderr\n",
                )
                wait_for(lambda: inspect(names[runner]) is None)
            if unreadable:
                pytest.skip(unreadable)
        finally:
            for directory in workdirs.values():
                (directory / "release").touch()
            monkeypatch.setenv("CONTAINER_RUNNER", "chsdk")
            _common.reap_container(names["chsdk"])
            subprocess.run(["docker", "rm", "-f", names["native"]],
                           capture_output=True, timeout=30)
