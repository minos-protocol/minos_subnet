"""Exercise command construction at every real validator container call site."""
import os
import subprocess

import pytest

pytest.importorskip("compute_horde_sdk")
from templates import _chsdk, bcftools, deepvariant, freebayes, gatk
from utils import scoring


@pytest.fixture
def inputs(tmp_path):
    bam, ref, out = (tmp_path / name for name in ("sample reads.bam", "ref.fa", "out.vcf.gz"))
    for path in (bam, ref, ref.with_suffix(".fa.fai"), bam.with_suffix(".bam.bai")):
        path.touch()
    return bam, ref, out


def capture(monkeypatch, module, after=None):
    calls = []
    def run(cmd, **kwargs):
        spec = _chsdk.parse_command(cmd)
        _chsdk._Output(kwargs)
        # Compare against the untouched suffix of the original argument list.
        image_index = cmd.index(spec.docker_image)
        assert spec.args == tuple(cmd[image_index + 1:])
        calls.append((spec, kwargs))
        if after:
            after(spec, kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(module, "run_container", run)
    return calls


@pytest.mark.parametrize("module,destinations", [
    (gatk, ["/data/bams", "/data/reference", "/data/output"]),
    (deepvariant, ["/input", "/reference", "/output"]),
])
def test_gatk_and_deepvariant(monkeypatch, inputs, module, destinations):
    calls = capture(monkeypatch, module)
    module.variant_call(*inputs, "chr20:1-1000", {"threads": 2, "memory_gb": 6, "timeout": 99})
    assert len(calls) == 1
    spec, kwargs = calls[0]
    assert [(v.source, v.destination, v.read_only) for v in spec.volumes] == [
        (inputs[0].parent, dest, ro) for dest, ro in zip(destinations, [True, True, False])]
    assert (spec.cpus, spec.memory, kwargs["timeout"]) == (2, "6g", 99)
    if module is deepvariant:
        assert any(a.startswith("--intermediate_results_dir=/output/") for a in spec.args)


@pytest.mark.parametrize("arch,platform", [("arm64", "linux/amd64"), ("x86_64", None)])
def test_bcftools_index_and_pipeline(monkeypatch, inputs, arch, platform):
    inputs[0].with_suffix(".bam.bai").unlink()
    monkeypatch.setattr(bcftools.platform, "machine", lambda: arch)
    calls = capture(monkeypatch, bcftools)
    bcftools.variant_call(*inputs, "chr20:1-1000", {"threads": 2})
    assert len(calls) == 2
    index, pipeline = (call[0] for call in calls)
    assert [(s.cpus, s.memory, s.platform) for s in (index, pipeline)] == [(2, None, platform)] * 2
    assert [(v.destination, v.read_only) for v in index.volumes] == [("/data", False)]
    assert [(v.destination, v.read_only) for v in pipeline.volumes] == [
        ("/data", False), ("/ref", False), ("/output", False)]
    assert pipeline.user == f"{os.getuid()}:{os.getgid()}"
    assert pipeline.args[:2] == ("sh", "-lc")
    assert len(pipeline.args) == 3 and " | bcftools call " in pipeline.args[2]
    assert "'sample reads.bam'" in pipeline.args[2]
    assert calls[0][1]["timeout"] == 300


@pytest.mark.parametrize("arch,platform", [("arm64", "linux/amd64"), ("x86_64", None)])
def test_freebayes_file_then_compression(monkeypatch, inputs, arch, platform):
    monkeypatch.setattr(freebayes.platform, "machine", lambda: arch)
    def output(spec, kwargs):
        if "stdout" in kwargs:
            kwargs["stdout"].write("##VCF\n" * 100)
    calls = capture(monkeypatch, freebayes, output)
    freebayes.variant_call(*inputs, "chr20:1-1000", {"threads": 2, "memory_gb": 6})
    assert len(calls) == 2
    call, compress = (c[0] for c in calls)
    assert [(s.cpus, s.memory, s.platform) for s in (call, compress)] == [(2, "6g", platform)] * 2
    assert [(v.destination, v.read_only) for v in call.volumes] == [("/data", True), ("/ref", True)]
    assert [(v.destination, v.read_only) for v in compress.volumes] == [("/data", False)]
    assert calls[0][1]["stderr"] == subprocess.PIPE
    assert calls[0][1]["stdout"].closed  # caller, not CHSDK, owns the file lifetime
    assert compress.args[:2] == ("sh", "-c") and len(compress.args) == 3
    assert calls[1][1]["timeout"] == 120


@pytest.mark.parametrize("same_directory", [True, False])
def test_truth_slice_and_index(monkeypatch, tmp_path, same_directory):
    source = tmp_path / "truth.vcf.gz"
    source.touch()
    source.with_suffix(".gz.csi").touch()
    target = (tmp_path if same_directory else tmp_path / "other") / "slice.vcf.gz"
    calls = capture(monkeypatch, scoring, lambda *args: target.touch())
    assert scoring.slice_truth_vcf(source, target, "chr20:1-1000")
    assert len(calls) == 2
    assert [c[1]["timeout"] for c in calls] == [300, 60]
    destinations = ["/data"] if same_directory else ["/source", "/target"]
    assert [v.destination for v in calls[0][0].volumes] == destinations
    for spec, _ in calls:
        assert all(not v.read_only for v in spec.volumes)
        assert (spec.cpus, spec.memory) == (None, None)
    expected_target = f"{destinations[-1]}/slice.vcf.gz"
    assert calls[0][0].args[-2:] == ("-o", expected_target)


@pytest.mark.parametrize("bed_location", [None, "truth", "separate"])
def test_happy_entrypoint_env_sdf_and_optional_bed(monkeypatch, tmp_path, bed_location):
    truth_dir, work, ref_dir, bed_dir = (tmp_path / p for p in ("truth", "work", "ref", "bed"))
    for directory in (truth_dir, work, ref_dir, bed_dir):
        directory.mkdir()
    truth, query, ref = truth_dir / "truth.vcf", work / "query.vcf", ref_dir / "ref.fa"
    for path in (truth, query, ref):
        path.write_text("# header\n")
    sdf = ref_dir / "sdf"
    sdf.mkdir()
    bed = None if bed_location is None else (truth_dir if bed_location == "truth" else bed_dir) / "confident.bed"
    if bed:
        bed.write_text("chr20\t0\t1000\n")
    monkeypatch.setattr(scoring, "generate_synthetic_regions_bed", lambda *args: False)
    calls = capture(monkeypatch, scoring)
    scoring.HappyScorer().score_vcf(str(truth), str(query), reference_fasta=str(ref),
                                  reference_sdf=str(sdf), confident_bed=str(bed) if bed else None)
    assert len(calls) == 1
    spec, kwargs = calls[0]
    assert spec.args[:2] == ("/data/truth/truth.vcf", "/data/output/query.vcf")
    assert spec.env == {"HGREF": "/data/reference/ref.fa", "RTG_MEM": "8g"}
    assert (spec.cpus, spec.memory, kwargs["timeout"]) == (None, None, 600)
    assert spec.args[spec.args.index("--threads") + 1] == "4"
    assert [(v.destination, v.read_only) for v in spec.volumes] == [
        ("/data/truth", False), ("/data/reference", False), ("/data/output", False),
        ("/data/sdf", True), *([("/data/bed", False)] if bed_location == "separate" else [])]


def test_happy_optional_query_mount_shape(tmp_path):
    # Current score_vcf sets output_dir=query.parent, so this branch is dormant.
    # Cover its exact mount/argv form without changing the scoring path.
    cmd = ["docker", "run", "--rm", "--name", "optional-query",
           "-v", f"{tmp_path}:/data/output", "-v", f"{tmp_path}:/data/query",
           "happy", "/data/truth/truth.vcf", "/data/query/query.vcf", "--threads", "4"]
    spec = _chsdk.parse_command(cmd)
    assert [(v.destination, v.read_only) for v in spec.volumes] == [("/data/output", False), ("/data/query", False)]
    assert spec.cpus is None
