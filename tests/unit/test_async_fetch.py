"""
Unit tests for src/ingestion/openml_fetch_async.py.

All HTTP calls are mocked via unittest.mock — no real network access.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ingestion.openml_fetch import FetchedDataset, FetchedRun
from src.ingestion.openml_fetch_async import (
    _clean_openml_description,
    _dataset_desc_cache,
    _flow_cache,
    _flow_desc_cache,
    fetch_dataset_async,
    fetch_dataset_description_async,
    fetch_flow_async,
    fetch_flow_description_async,
    fetch_runs_for_dataset_async,
    fetch_task_async,
    list_dataset_ids_async,
    reset_semaphore,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _xml_response(body: str) -> MagicMock:
    """Build a mock httpx.Response that returns XML text."""
    mock_resp = MagicMock()
    mock_resp.text = body
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _make_client(xml_body: str) -> MagicMock:
    """Return a mock httpx.AsyncClient whose .get() always returns xml_body."""
    mock_resp = _xml_response(xml_body)
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)
    return client


DATASET_META_XML = """
<oml:data_set_description xmlns:oml="http://openml.org/openml">
  <oml:id>61</oml:id>
  <oml:name>iris</oml:name>
</oml:data_set_description>
"""

DATASET_QUALITIES_XML = """
<oml:data_qualities xmlns:oml="http://openml.org/openml">
  <oml:quality>
    <oml:name>NumberOfInstances</oml:name>
    <oml:value>150.0</oml:value>
  </oml:quality>
  <oml:quality>
    <oml:name>NumberOfFeatures</oml:name>
    <oml:value>5.0</oml:value>
  </oml:quality>
  <oml:quality>
    <oml:name>NumberOfClasses</oml:name>
    <oml:value>3.0</oml:value>
  </oml:quality>
  <oml:quality>
    <oml:name>MajorityClassSize</oml:name>
    <oml:value>50.0</oml:value>
  </oml:quality>
  <oml:quality>
    <oml:name>MinorityClassSize</oml:name>
    <oml:value>50.0</oml:value>
  </oml:quality>
</oml:data_qualities>
"""

FLOW_XML = """
<oml:flow xmlns:oml="http://openml.org/openml">
  <oml:id>67</oml:id>
  <oml:name>weka.BayesNet_K2</oml:name>
</oml:flow>
"""

TASK_LIST_XML = """
<oml:tasks xmlns:oml="http://openml.org/openml">
  <oml:task>
    <oml:task_id>59</oml:task_id>
    <oml:task_type_id>1</oml:task_type_id>
    <oml:task_type>Supervised Classification</oml:task_type>
    <oml:did>61</oml:did>
    <oml:name>iris</oml:name>
  </oml:task>
  <oml:task>
    <oml:task_id>289</oml:task_id>
    <oml:task_type_id>1</oml:task_type_id>
    <oml:task_type>Supervised Classification</oml:task_type>
    <oml:did>61</oml:did>
    <oml:name>iris</oml:name>
  </oml:task>
</oml:tasks>
"""

TASK_LIST_GLOBAL_XML = """
<oml:tasks xmlns:oml="http://openml.org/openml">
  <oml:task>
    <oml:task_id>2</oml:task_id>
    <oml:task_type_id>1</oml:task_type_id>
    <oml:did>2</oml:did>
  </oml:task>
  <oml:task>
    <oml:task_id>3</oml:task_id>
    <oml:task_type_id>1</oml:task_type_id>
    <oml:did>2</oml:did>
  </oml:task>
  <oml:task>
    <oml:task_id>10</oml:task_id>
    <oml:task_type_id>1</oml:task_type_id>
    <oml:did>61</oml:did>
  </oml:task>
</oml:tasks>
"""

RUN_LIST_XML = """
<oml:runs xmlns:oml="http://openml.org/openml">
  <oml:run>
    <oml:run_id>81</oml:run_id>
    <oml:task_id>59</oml:task_id>
    <oml:setup_id>12</oml:setup_id>
    <oml:flow_id>67</oml:flow_id>
  </oml:run>
  <oml:run>
    <oml:run_id>161</oml:run_id>
    <oml:task_id>59</oml:task_id>
    <oml:setup_id>13</oml:setup_id>
    <oml:flow_id>70</oml:flow_id>
  </oml:run>
</oml:runs>
"""

TASK_DETAIL_XML = """
<oml:task xmlns:oml="http://openml.org/openml">
  <oml:task>
    <oml:task_id>59</oml:task_id>
    <oml:task_type>Supervised Classification</oml:task_type>
    <oml:input name="target_feature">class</oml:input>
    <oml:input name="evaluation_measures">predictive_accuracy</oml:input>
  </oml:task>
</oml:task>
"""


DATASET_WITH_DESC_XML = """
<oml:data_set_description xmlns:oml="http://openml.org/openml">
  <oml:id>40668</oml:id>
  <oml:name>connect-4</oml:name>
  <oml:description>Connect Four board game winning positions. Two players alternate dropping
    discs into a 7-column, 6-row grid. The goal is to connect four discs of the same colour
    horizontally, vertically, or diagonally.</oml:description>
</oml:data_set_description>
"""

FLOW_WITH_DESC_XML = """
<oml:flow xmlns:oml="http://openml.org/openml">
  <oml:id>6969</oml:id>
  <oml:name>sklearn.ensemble._hist_gradient_boosting.gradient_boosting.HistGradientBoostingClassifier</oml:name>
  <oml:description>Histogram-based gradient boosting classification tree. Uses histogram
    binning to discretise continuous features, which dramatically speeds up training
    compared to the standard GradientBoostingClassifier.</oml:description>
</oml:flow>
"""


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_flow_cache():
    """Clear the module-level flow cache and description caches between tests."""
    _flow_cache.clear()
    _dataset_desc_cache.clear()
    _flow_desc_cache.clear()
    reset_semaphore(8)
    yield
    _flow_cache.clear()
    _dataset_desc_cache.clear()
    _flow_desc_cache.clear()


# ── Tests: list_dataset_ids_async ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_dataset_ids_async_basic():
    """Parses task list XML and groups by dataset_id."""
    client = _make_client(TASK_LIST_GLOBAL_XML)
    result = await list_dataset_ids_async(client, "Supervised Classification", 10, 1)
    # did=2 appears twice, did=61 once — did=2 should be first
    assert result[0] == 2
    assert 61 in result
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_dataset_ids_async_respects_max():
    """max_datasets caps the returned list."""
    client = _make_client(TASK_LIST_GLOBAL_XML)
    result = await list_dataset_ids_async(client, "Supervised Classification", 1, 1)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_list_dataset_ids_async_returns_empty_on_http_error():
    """Returns empty list when the HTTP call fails."""
    client = MagicMock()
    import httpx
    client.get = AsyncMock(side_effect=httpx.HTTPError("connection failed"))
    result = await list_dataset_ids_async(client, "Supervised Classification", 10, 1)
    assert result == []


# ── Tests: fetch_dataset_async ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_dataset_async_parses_name_and_qualities():
    """Parses dataset name and quality values from XML."""
    call_count = 0

    async def mock_get(url: str, **kwargs):
        nonlocal call_count
        call_count += 1
        if "/data/qualities/" in url:
            return _xml_response(DATASET_QUALITIES_XML)
        return _xml_response(DATASET_META_XML)

    client = MagicMock()
    client.get = mock_get

    result = await fetch_dataset_async(client, 61)

    assert result is not None
    assert isinstance(result, FetchedDataset)
    assert result.dataset_id == 61
    assert result.name == "iris"
    assert result.qualities["NumberOfInstances"] == "150.0"
    assert result.qualities["NumberOfClasses"] == "3.0"
    # Both meta and qualities endpoints were called
    assert call_count == 2


@pytest.mark.asyncio
async def test_fetch_dataset_async_returns_none_on_404():
    """Returns None if dataset metadata endpoint returns 404."""
    import httpx

    async def mock_get(url: str, **kwargs):
        if "/data/qualities/" in url:
            return _xml_response(DATASET_QUALITIES_XML)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=MagicMock(), response=MagicMock(status_code=404)
            )
        )
        return mock_resp

    client = MagicMock()
    client.get = mock_get

    result = await fetch_dataset_async(client, 9999)
    assert result is None


# ── Tests: fetch_flow_async ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_flow_async_parses_name():
    """Parses flow name from XML."""
    client = _make_client(FLOW_XML)
    result = await fetch_flow_async(client, 67)

    assert result is not None
    assert result["flow_id"] == 67
    assert result["name"] == "weka.BayesNet_K2"


@pytest.mark.asyncio
async def test_fetch_flow_async_cache_hit_avoids_http():
    """Second call for the same flow_id skips HTTP (cache hit)."""
    client = _make_client(FLOW_XML)

    result1 = await fetch_flow_async(client, 67)
    result2 = await fetch_flow_async(client, 67)

    # get() called exactly once (for the first fetch)
    assert client.get.call_count == 1
    assert result1 == result2


@pytest.mark.asyncio
async def test_fetch_flow_async_404_returns_unknown():
    """404 response yields unknown_flow_{id} name and is cached."""
    import httpx

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)

    result = await fetch_flow_async(client, 9999)
    assert result is not None
    assert result["name"] == "unknown_flow_9999"

    # Second call should hit cache, not re-raise
    result2 = await fetch_flow_async(client, 9999)
    assert result2["name"] == "unknown_flow_9999"
    assert client.get.call_count == 1  # still only one HTTP hit


@pytest.mark.asyncio
async def test_fetch_flow_batch_404_does_not_break_others():
    """In a gather, a 404 for one flow does not prevent others from succeeding."""
    import httpx

    async def mock_get(url: str, **kwargs):
        if "/flow/9999" in url:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=MagicMock(status_code=404)
                )
            )
            return mock_resp
        return _xml_response(FLOW_XML)

    client = MagicMock()
    client.get = mock_get

    results = await asyncio.gather(
        fetch_flow_async(client, 9999),
        fetch_flow_async(client, 67),
        return_exceptions=True,
    )
    # Both should resolve (no exception raised to caller)
    assert not any(isinstance(r, Exception) for r in results)
    unknown = next(r for r in results if r["flow_id"] == 9999)
    known = next(r for r in results if r["flow_id"] == 67)
    assert unknown["name"] == "unknown_flow_9999"
    assert known["name"] == "weka.BayesNet_K2"


# ── Tests: fetch_runs_for_dataset_async ───────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_runs_for_dataset_async_basic():
    """Returns FetchedRun objects parsed from run list XML."""
    call_count = 0

    async def mock_get(url: str, **kwargs):
        nonlocal call_count
        call_count += 1
        # task list for dataset
        if "/task/list/type/1/data_id/" in url:
            return _xml_response(TASK_LIST_XML)
        # run list
        if "/run/list/" in url:
            return _xml_response(RUN_LIST_XML)
        return _xml_response("<oml:error/>")

    client = MagicMock()
    client.get = mock_get

    runs = await fetch_runs_for_dataset_async(client, 61, "Supervised Classification")

    # 2 tasks * 2 runs each = 4 (RUN_LIST_XML has 2 runs, fetched for each task)
    assert len(runs) == 4
    assert all(isinstance(r, FetchedRun) for r in runs)
    assert all(r.dataset_id == 61 for r in runs)


@pytest.mark.asyncio
async def test_fetch_runs_for_dataset_async_respects_max_runs():
    """max_runs parameter caps the total returned."""
    async def mock_get(url: str, **kwargs):
        if "/task/list/type/1/data_id/" in url:
            return _xml_response(TASK_LIST_XML)
        if "/run/list/" in url:
            return _xml_response(RUN_LIST_XML)
        return _xml_response("<oml:error/>")

    client = MagicMock()
    client.get = mock_get

    runs = await fetch_runs_for_dataset_async(
        client, 61, "Supervised Classification", max_runs=3
    )
    assert len(runs) <= 3


# ── Tests: semaphore concurrency ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semaphore_limits_parallelism():
    """At most `limit` concurrent HTTP calls run at the same time."""
    import asyncio

    reset_semaphore(3)
    concurrent_peak = 0
    current = 0
    lock = asyncio.Lock()

    async def slow_get(url: str, **kwargs):
        nonlocal concurrent_peak, current
        async with lock:
            current += 1
            if current > concurrent_peak:
                concurrent_peak = current
        await asyncio.sleep(0.01)
        async with lock:
            current -= 1
        return _xml_response(FLOW_XML)

    client = MagicMock()
    client.get = slow_get

    # Launch 10 concurrent fetch_flow_async calls for distinct IDs
    await asyncio.gather(*[
        fetch_flow_async(client, 1000 + i) for i in range(10)
    ])

    # With semaphore(3), peak should be ≤ 3
    assert concurrent_peak <= 3


# ── Tests: fetch_task_async ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_task_async_parses_fields():
    """Parses task_type, target_feature, evaluation_measure from XML."""
    client = _make_client(TASK_DETAIL_XML)
    result = await fetch_task_async(client, 59)

    assert result is not None
    assert result["task_id"] == 59
    assert result["task_type"] == "Supervised Classification"
    assert result["target_feature"] == "class"
    assert result["evaluation_measure"] == "predictive_accuracy"


@pytest.mark.asyncio
async def test_fetch_task_async_returns_none_on_failure():
    """Returns None if HTTP call fails."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=Exception("timeout"))
    result = await fetch_task_async(client, 999)
    assert result is None


# ── Tests: _clean_openml_description ─────────────────────────────────────────

def test_clean_openml_description_collapses_whitespace():
    """Newlines and multiple spaces are collapsed to single spaces."""
    raw = "Hello\n  world\t  test"
    result = _clean_openml_description(raw)
    assert result == "Hello world test"


def test_clean_openml_description_truncates_at_word_boundary():
    """Long descriptions are truncated to max_chars, breaking on a word."""
    raw = " ".join(["word"] * 300)  # ~1500 chars
    result = _clean_openml_description(raw, max_chars=50)
    assert len(result) <= 50
    # Should not end mid-word
    assert result == result.strip()


def test_clean_openml_description_short_text_unchanged():
    """Short descriptions pass through unchanged."""
    raw = "A brief description."
    assert _clean_openml_description(raw) == raw


# ── Tests: fetch_dataset_description_async ────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_dataset_description_async_parses_description():
    """Parses <oml:description> from dataset metadata XML."""
    client = _make_client(DATASET_WITH_DESC_XML)
    result = await fetch_dataset_description_async(client, 40668)

    assert isinstance(result, str)
    assert len(result) > 0
    # Should contain connect-four vocabulary after cleaning
    assert "connect" in result.lower() or "board" in result.lower()


@pytest.mark.asyncio
async def test_fetch_dataset_description_async_caches_result():
    """Second call for same dataset_id returns cached result without HTTP."""
    client = _make_client(DATASET_WITH_DESC_XML)

    r1 = await fetch_dataset_description_async(client, 40668)
    r2 = await fetch_dataset_description_async(client, 40668)

    assert client.get.call_count == 1
    assert r1 == r2


@pytest.mark.asyncio
async def test_fetch_dataset_description_async_returns_empty_on_404():
    """Returns empty string when HTTP returns 404."""
    import httpx

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)

    result = await fetch_dataset_description_async(client, 9999)
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_dataset_description_async_returns_empty_on_missing_element():
    """Returns empty string when <oml:description> element is absent."""
    xml_no_desc = """
    <oml:data_set_description xmlns:oml="http://openml.org/openml">
      <oml:id>61</oml:id>
      <oml:name>iris</oml:name>
    </oml:data_set_description>
    """
    client = _make_client(xml_no_desc)
    result = await fetch_dataset_description_async(client, 61)
    assert result == ""


# ── Tests: fetch_flow_description_async ──────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_flow_description_async_parses_description():
    """Parses <oml:description> from flow XML including histogram vocabulary."""
    client = _make_client(FLOW_WITH_DESC_XML)
    result = await fetch_flow_description_async(client, 6969)

    assert isinstance(result, str)
    assert len(result) > 0
    # Key vocabulary that was missing before
    assert "histogram" in result.lower() or "bin" in result.lower()


@pytest.mark.asyncio
async def test_fetch_flow_description_async_caches_result():
    """Second call for same flow_id returns cached result without HTTP."""
    client = _make_client(FLOW_WITH_DESC_XML)

    r1 = await fetch_flow_description_async(client, 6969)
    r2 = await fetch_flow_description_async(client, 6969)

    assert client.get.call_count == 1
    assert r1 == r2


@pytest.mark.asyncio
async def test_fetch_flow_description_async_returns_empty_on_404():
    """Returns empty string when HTTP returns 404."""
    import httpx

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)

    result = await fetch_flow_description_async(client, 9999)
    assert result == ""


# ── Tests: enriched description format ───────────────────────────────────────

def test_enriched_description_combines_synthesised_and_openml():
    """Combined description format contains both synthesised and OpenML vocabulary."""
    from src.ingestion.transform import synthesise_description

    # Simulate what loader_async does when writing a new Algorithm node.
    algo_props = {
        "name": "sklearn.ensemble.HistGradientBoostingClassifier",
        "display_name": "HistGradientBoostingClassifier",
        "family": "gradient_boosting",
    }
    synthesised = synthesise_description("Algorithm", algo_props)
    openml_text = (
        "Histogram-based gradient boosting. Uses histogram binning to discretise "
        "continuous features into fixed-width bins."
    )
    combined = synthesised + " " + openml_text

    # The combined description must contain both original family synonyms
    # and the new histogram/bins vocabulary.
    assert "gradient boosting" in combined.lower()
    assert "histogram" in combined.lower()
    assert "bin" in combined.lower()


def test_enriched_description_connect_four_vocabulary():
    """Combined description for connect-4 should contain board-game vocabulary."""
    from src.ingestion.transform import synthesise_description

    ds_props = {
        "name": "connect-4",
        "n_rows": 67557,
        "n_features": 42,
        "n_classes": 3,
        "imbalance_ratio": 1.4,
    }
    synthesised = synthesise_description("Dataset", ds_props)
    openml_text = (
        "Connect Four board game positions. Two players alternate dropping discs "
        "into a 7-column 6-row grid. Detect winning positions for player one."
    )
    combined = synthesised + " " + openml_text

    assert "connect" in combined.lower()
    assert "board" in combined.lower() or "game" in combined.lower()
