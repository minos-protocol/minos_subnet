"""hap.py artifacts must belong to the invocation being scored.

The output prefix is derived from the query filename and the round directory is
reused per hotkey, so a retry or restart finds the previous invocation's files
at exactly the same paths. Accepting them makes a failed scoring attempt look
successful, and can pair a fresh artifact with a stale one.

These drive the real `HappyScorer.score_vcf` with hap.py itself stubbed out —
the guard is inside that method, so a test that reimplements the check proves
nothing about whether the method still performs it.
"""
import gzip
import os
import time
from pathlib import Path

import pytest

from utils.scoring import HappyScorer

SUMMARY = (
    "Type,Filter,TRUTH.TOTAL,TRUTH.TP,TRUTH.FN,QUERY.TOTAL,QUERY.FP,QUERY.UNK,"
    "FP.gt,FP.al,METRIC.Recall,METRIC.Precision,METRIC.Frac_NA,METRIC.F1_Score,"
    "TRUTH.TOTAL.TiTv_ratio,QUERY.TOTAL.TiTv_ratio,TRUTH.TOTAL.het_hom_ratio,"
    "QUERY.TOTAL.het_hom_ratio\n"
    "SNP,ALL,100,100,0,100,0,0,0,0,1.0,1.0,0.0,1.0,2.0,2.0,1.5,1.5\n"
    "INDEL,ALL,10,10,0,10,0,0,0,0,1.0,1.0,0.0,1.0,,,1.5,1.5\n"
)


class _Completed:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, "", stderr


@pytest.fixture
def workdir(tmp_path):
    """A round directory holding a truth and query VCF, as scoring sees it.

    Also an SDF directory: score_vcf refuses to run without one, and that
    refusal is indistinguishable from the stale-artifact refusal these tests
    are about.
    """
    for name in ("truth.vcf", "q.vcf"):
        (tmp_path / name).write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\n")
    (tmp_path / "sdf").mkdir()
    return tmp_path


def _write_artifacts(prefix: Path, *, age_seconds=0.0):
    """A complete, valid-looking artifact set at hap.py's output prefix."""
    summary = Path(f"{prefix}.summary.csv")
    vcf = Path(f"{prefix}.vcf.gz")
    summary.write_text(SUMMARY)
    # a real gzip: the annotated VCF is parsed, so a text placeholder fails for
    # the wrong reason and hides whether the guard fired
    with gzip.open(vcf, "wt") as fh:
        fh.write("##fileformat=VCFv4.2\n"
                 "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(summary, (old, old))
        os.utime(vcf, (old, old))
    return summary, vcf


def _run(monkeypatch, workdir, *, hap_exit=0, leave_stale=False, produce=True):
    """Drive score_vcf with hap.py replaced by a stub."""
    scorer = HappyScorer()
    query = workdir / "q.vcf"
    prefix = workdir / f"happy_{query.stem}"

    if leave_stale:
        _write_artifacts(prefix, age_seconds=3600)

    def fake_run(cmd, **kw):
        if produce:
            _write_artifacts(prefix)
        return _Completed(hap_exit)

    monkeypatch.setattr("utils.scoring.subprocess.run", fake_run)
    return scorer.score_vcf(str(workdir / "truth.vcf"), str(query),
                            reference_sdf=str(workdir / "sdf"))


def _refused(result):
    """score_vcf refuses by returning None (`_get_zero_scores`). A scored run
    returns a dict — possibly with zero F1, which is a low score, not a
    refusal. Conflating the two is how a guard looks tested when it is not."""
    return result is None


class TestStaleArtifactsAreNotScored:
    def test_a_stale_set_is_not_scored_as_this_runs_result(self, monkeypatch, workdir):
        """hap.py leaves the previous invocation's files in place and produces
        nothing. Those must not be read as this run's output."""
        result = _run(monkeypatch, workdir, leave_stale=True, produce=False)
        assert _refused(result), "a previous invocation's artifacts were scored"

    def test_a_fresh_run_is_scored_normally(self, monkeypatch, workdir):
        """The guard must not reject a genuine result — otherwise it would be
        indistinguishable from one that simply never scores."""
        result = _run(monkeypatch, workdir, produce=True)
        assert not _refused(result), "a fresh, complete artifact set was refused"
        assert isinstance(result, dict) and "f1_snp" in result

    def test_stale_artifacts_are_purged_before_the_run(self, monkeypatch, workdir):
        """Clearing the prefix first makes 'the file exists' mean 'this run
        produced it'."""
        prefix = workdir / "happy_q"
        summary, _ = _write_artifacts(prefix, age_seconds=3600)
        stale_mtime = summary.stat().st_mtime
        _run(monkeypatch, workdir, produce=True)
        assert summary.stat().st_mtime > stale_mtime, "the stale summary survived"

    def test_a_nonzero_exit_without_a_complete_set_is_refused(self, monkeypatch, workdir):
        """A nonzero exit is tolerable only when this run produced everything.
        Without the annotated VCF the v2 core cannot be built, and a partial run
        must not read as a successful one."""
        scorer = HappyScorer()
        query = workdir / "q.vcf"
        prefix = workdir / f"happy_{query.stem}"

        def fake_run(cmd, **kw):
            Path(f"{prefix}.summary.csv").write_text(SUMMARY)   # summary only
            return _Completed(1, stderr="hap.py died")

        monkeypatch.setattr("utils.scoring.subprocess.run", fake_run)
        result = scorer.score_vcf(str(workdir / "truth.vcf"), str(query),
                                  reference_sdf=str(workdir / "sdf"))
        assert _refused(result), "a partial artifact set was scored"

    def test_the_freshness_check_holds_when_the_purge_silently_fails(
        self, monkeypatch, workdir
    ):
        """The purge is the primary mechanism; the freshness check is there for
        an unlink that failed without raising. With the purge defeated, a stale
        set must still be refused — otherwise the second layer is decoration.
        """
        monkeypatch.setattr(Path, "unlink", lambda self, **kw: None)
        result = _run(monkeypatch, workdir, leave_stale=True, produce=False)
        assert _refused(result), "a stale set survived a failed purge and was scored"

    def test_the_purge_holds_when_the_freshness_check_is_defeated(
        self, monkeypatch, workdir
    ):
        """And the reverse: with every file looking fresh, the purge alone must
        still have removed the previous run's output."""
        real_stat = Path.stat
        monkeypatch.setattr(Path, "stat", lambda self, **kw: _AlwaysFresh(real_stat(self)))
        result = _run(monkeypatch, workdir, leave_stale=True, produce=False)
        assert _refused(result), "a stale set survived with the freshness check defeated"


class _AlwaysFresh:
    """A stat result whose mtime is always now."""
    def __init__(self, real):
        self._real = real

    @property
    def st_mtime(self):
        return time.time()

    def __getattr__(self, name):
        return getattr(self._real, name)
