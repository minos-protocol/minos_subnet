"""Tests for the fp_per_target denominator: one synthetic target counts once,
however many query records represent it."""
from pathlib import Path


from utils.scoring import compute_synthetic_only_metrics

MUT_HEADER = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
HAPPY_HEADER = ("##fileformat=VCFv4.2\n"
                "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTRUTH\tQUERY\n")


def _write(p, text):
    Path(p).write_text(text)
    return str(p)


def _mut(rows):
    return MUT_HEADER + "".join(f"{c}\t{p}\t.\t{r}\t{a}\t50\tPASS\t.\n" for c, p, r, a in rows)


def _happy(rows):
    out = HAPPY_HEADER
    for c, p, r, a, bdt, bdq, bvt in rows:
        out += (f"{c}\t{p}\t.\t{r}\t{a}\t50\tPASS\t.\tGT:BD:BVT\t"
                f"1/1:{bdt}:{bvt}\t1/1:{bdq}:{bvt}\n")
    return out


class TestTheDenominatorIsQueryIndependent:
    """The denominator is counted from the mutations VCF, not reconstructed
    from hap.py output: after +/-10bp non-consuming matching several decomposed
    query records can match one target, which would inflate a TP+FN count."""

    MUT = [("chr20", 1000, "AT", "A")]   # one indel target

    def test_one_target_counts_once_however_many_records_match(self, tmp_path):
        mut = _write(tmp_path / "m.vcf", _mut(self.MUT))

        # three hap.py records all within 10bp of the single target
        many = _write(tmp_path / "h1.vcf", _happy([
            ("chr20", 998, "AT", "A", "TP", "TP", "INDEL"),
            ("chr20", 1000, "AT", "A", "TP", "TP", "INDEL"),
            ("chr20", 1004, "AT", "A", "TP", "TP", "INDEL"),
        ]))
        one = _write(tmp_path / "h2.vcf", _happy([
            ("chr20", 1000, "AT", "A", "TP", "TP", "INDEL"),
        ]))

        m_many = compute_synthetic_only_metrics(many, mut)
        m_one = compute_synthetic_only_metrics(one, mut)
        assert m_many and m_one

        assert m_many["target_total_indel"] == m_one["target_total_indel"] == 1.0, (
            "the target count moved with how many query records matched it"
        )
        # truth_total_indel is tp+fn counted from the hap.py records, so it rises
        # with how many of them matched the one target -- hence >=, not ==.
        assert m_many["truth_total_indel"] >= m_one["truth_total_indel"]

    def test_multiallelic_targets_count_per_alt_allele(self, tmp_path):
        mut = _write(tmp_path / "m.vcf", _mut([("chr20", 500, "A", "G,T")]))
        happy = _write(tmp_path / "h.vcf", _happy([]))
        m = compute_synthetic_only_metrics(happy, mut)
        assert m and m["target_total_snp"] == 2.0, (
            "a multiallelic record must count its ALT alleles explicitly"
        )

    def test_the_same_variant_written_two_ways_gives_one_denominator(self, tmp_path):
        """Left-aligned vs shifted representation of the same deletion."""
        mut = _write(tmp_path / "m.vcf", _mut(self.MUT))
        rep_a = _write(tmp_path / "a.vcf", _happy([
            ("chr20", 1000, "AT", "A", "TP", "TP", "INDEL")]))
        rep_b = _write(tmp_path / "b.vcf", _happy([
            ("chr20", 1002, "TA", "T", "TP", "TP", "INDEL")]))
        a = compute_synthetic_only_metrics(rep_a, mut)
        b = compute_synthetic_only_metrics(rep_b, mut)
        assert a["target_total_indel"] == b["target_total_indel"] == 1.0
