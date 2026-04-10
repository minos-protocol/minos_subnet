"""Shared fixtures for Minos test suite."""

import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    """Path to test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def score_tracker():
    """Fresh ScoreTracker with default settings."""
    from utils.weight_tracking import ScoreTracker
    return ScoreTracker(alpha=0.1, min_rounds=10, decay_factor=0.95)


@pytest.fixture
def score_tracker_low_threshold():
    """ScoreTracker with low eligibility threshold for faster tests."""
    from utils.weight_tracking import ScoreTracker
    return ScoreTracker(alpha=0.1, min_rounds=2, decay_factor=0.95)


@pytest.fixture
def sample_happy_metrics():
    """Realistic hap.py metrics for AdvancedScorer tests."""
    return {
        'f1_snp': 0.95,
        'f1_indel': 0.90,
        'recall_snp': 0.95,
        'recall_indel': 0.90,
        'precision_snp': 0.95,
        'precision_indel': 0.90,
        'truth_total_snp': 100,
        'truth_total_indel': 50,
        'query_total_snp': 105,
        'query_total_indel': 55,
        'fp_snp': 5,
        'fp_indel': 5,
        'frac_na_snp': 0.0,
        'frac_na_indel': 0.0,
        'titv_truth_snp': 2.1,
        'titv_query_snp': 2.0,
        'hethom_truth_snp': 1.5,
        'hethom_query_snp': 1.6,
        'hethom_truth_indel': 1.5,
        'hethom_query_indel': 1.4,
    }


@pytest.fixture
def zero_happy_metrics():
    """All-zero hap.py metrics."""
    return {
        'f1_snp': 0.0,
        'f1_indel': 0.0,
        'recall_snp': 0.0,
        'recall_indel': 0.0,
        'precision_snp': 0.0,
        'precision_indel': 0.0,
        'truth_total_snp': 100,
        'truth_total_indel': 50,
        'query_total_snp': 0,
        'query_total_indel': 0,
        'fp_snp': 0,
        'fp_indel': 0,
        'frac_na_snp': 0.0,
        'frac_na_indel': 0.0,
        'titv_truth_snp': 2.1,
        'titv_query_snp': 0.0,
        'hethom_truth_snp': 1.5,
        'hethom_query_snp': 0.0,
        'hethom_truth_indel': 1.5,
        'hethom_query_indel': 0.0,
    }


@pytest.fixture
def perfect_happy_metrics():
    """Perfect hap.py metrics (all F1=1.0, zero FP)."""
    return {
        'f1_snp': 1.0,
        'f1_indel': 1.0,
        'recall_snp': 1.0,
        'recall_indel': 1.0,
        'precision_snp': 1.0,
        'precision_indel': 1.0,
        'truth_total_snp': 100,
        'truth_total_indel': 50,
        'query_total_snp': 100,
        'query_total_indel': 50,
        'fp_snp': 0,
        'fp_indel': 0,
        'frac_na_snp': 0.0,
        'frac_na_indel': 0.0,
        'titv_truth_snp': 2.1,
        'titv_query_snp': 2.1,
        'hethom_truth_snp': 1.5,
        'hethom_query_snp': 1.5,
        'hethom_truth_indel': 1.5,
        'hethom_query_indel': 1.5,
    }


@pytest.fixture
def happy_summary_csv_path(fixtures_dir):
    """Path to happy_summary.csv fixture."""
    return fixtures_dir / "happy_summary.csv"
