"""
Unit tests for pure helper functions in src/ui/app.py.

These tests do not import Streamlit state — they only exercise:
  - highlight_citations(answer) -> HTML string
  - format_stats(stats)        -> display dict
"""

from __future__ import annotations

import html

# Import helpers directly — they do not require Streamlit to be running.
from src.ui.app import format_stats, highlight_citations

# ---------------------------------------------------------------------------
# highlight_citations
# ---------------------------------------------------------------------------


def test_highlight_wraps_algorithm_citation():
    result = highlight_citations("See [Algorithm: RandomForest] for details.")
    assert '<span style="background:#fff4a3' in result
    assert "[Algorithm: RandomForest]" in result


def test_highlight_wraps_dataset_citation():
    result = highlight_citations("Benchmark: [Dataset: iris].")
    assert "[Dataset: iris]" in result
    assert "background:#fff4a3" in result


def test_highlight_wraps_run_citation():
    result = highlight_citations("[Run: 123456] achieved 0.94.")
    assert "[Run: 123456]" in result
    assert "background:#fff4a3" in result


def test_highlight_wraps_task_citation():
    result = highlight_citations("[Task: Supervised Classification] task.")
    assert "[Task: Supervised Classification]" in result
    assert "background:#fff4a3" in result


def test_highlight_multiple_citations():
    text = "[Algorithm: SVM] on [Dataset: digits] in [Run: 99]"
    result = highlight_citations(text)
    assert result.count("background:#fff4a3") == 3


def test_highlight_no_citations_passthrough():
    text = "No citations here."
    result = highlight_citations(text)
    assert result == html.escape(text)


def test_highlight_escapes_html_in_answer():
    """Model output with HTML tags must be escaped to prevent XSS."""
    malicious = "<script>alert('xss')</script> See [Algorithm: SVM]."
    result = highlight_citations(malicious)
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    # Citation should still be highlighted
    assert "[Algorithm: SVM]" in result


def test_highlight_escapes_angle_brackets():
    text = "Score > 0.9 and < 1.0 for [Dataset: iris]."
    result = highlight_citations(text)
    assert "&gt;" in result
    assert "&lt;" in result
    assert "[Dataset: iris]" in result


def test_highlight_ampersand_escaped():
    text = "A & B on [Run: 42]."
    result = highlight_citations(text)
    assert "&amp;" in result
    assert "[Run: 42]" in result


# ---------------------------------------------------------------------------
# format_stats
# ---------------------------------------------------------------------------


def test_format_stats_returns_expected_keys():
    stats = {
        "algorithm_count": 189,
        "dataset_count": 3,
        "task_count": 3,
        "run_count": 600,
    }
    result = format_stats(stats)
    assert result["Algorithms"] == 189
    assert result["Datasets"] == 3
    assert result["Tasks"] == 3
    assert result["Runs"] == 600


def test_format_stats_defaults_to_zero_for_missing_keys():
    result = format_stats({})
    assert result["Algorithms"] == 0
    assert result["Runs"] == 0


def test_format_stats_preserves_zero_counts():
    stats = {"algorithm_count": 0, "dataset_count": 0, "task_count": 0, "run_count": 0}
    result = format_stats(stats)
    assert result["Runs"] == 0
