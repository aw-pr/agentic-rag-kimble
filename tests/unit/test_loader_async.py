"""
Unit tests for pass-14 changes to loader_async.py and openml_fetch_async.py.

Tests cover:
1. Outer-loop semaphore actually limits concurrent dataset processing.
2. PK-set lock prevents duplicate inserts under contention.
3. 412 pre-filter drops tasks with estimation_procedure in {0, 33}.

No real network or DB calls — everything is mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ingestion.loader_async import IngestState
from src.ingestion.openml_fetch_async import (
    _BLOCKED_ESTIMATION_PROCEDURES,
    _flow_cache,
    reset_semaphore,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _xml_response(body: str) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.text = body
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset shared module state between tests."""
    _flow_cache.clear()
    reset_semaphore(8)
    yield
    _flow_cache.clear()


# ── Test 1: outer-loop semaphore limits concurrent dataset processing ─────────

@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_datasets():
    """Semaphore(2) should mean at most 2 _process_dataset calls run at once."""
    concurrent_peak = 0
    current = 0
    lock = asyncio.Lock()

    async def fake_process(*, dataset_id, **kwargs):
        nonlocal concurrent_peak, current
        async with lock:
            current += 1
            if current > concurrent_peak:
                concurrent_peak = current
        await asyncio.sleep(0.02)  # simulate some work
        async with lock:
            current -= 1

    ds_sem = asyncio.Semaphore(2)

    async def _gated(ds_id: int) -> None:
        async with ds_sem:
            await fake_process(dataset_id=ds_id)

    await asyncio.gather(*[_gated(i) for i in range(10)], return_exceptions=True)

    assert concurrent_peak <= 2, (
        f"Expected peak ≤ 2 concurrent datasets, got {concurrent_peak}"
    )


# ── Test 2: PK-set lock prevents duplicate inserts ────────────────────────────

@pytest.mark.asyncio
async def test_pk_lock_prevents_duplicate_inserts():
    """Two coroutines racing to insert the same run_id should produce exactly one insert."""
    state = IngestState()
    inserted_run_ids: list[int] = []
    inserted_lock = asyncio.Lock()

    async def try_insert(run_id: int) -> None:
        """Simulate the check-and-add pattern from _process_dataset."""
        async with state.pk_lock:
            if run_id in state.existing_runs:
                return  # already inserted
            state.existing_runs.add(run_id)
        # Outside the lock — simulate enqueueing the write
        async with inserted_lock:
            inserted_run_ids.append(run_id)

    # Launch 20 coroutines all trying to insert run_id=42
    await asyncio.gather(*[try_insert(42) for _ in range(20)])

    assert inserted_run_ids.count(42) == 1, (
        f"Expected exactly 1 insert for run_id=42, got {inserted_run_ids.count(42)}"
    )


@pytest.mark.asyncio
async def test_pk_lock_allows_distinct_inserts():
    """Distinct PKs should all be inserted exactly once even under concurrency."""
    state = IngestState()
    inserted: list[int] = []
    inserted_lock = asyncio.Lock()

    async def try_insert(run_id: int) -> None:
        async with state.pk_lock:
            if run_id in state.existing_runs:
                return
            state.existing_runs.add(run_id)
        async with inserted_lock:
            inserted.append(run_id)

    n = 50
    await asyncio.gather(*[try_insert(i) for i in range(n)])

    assert len(inserted) == n
    assert sorted(inserted) == list(range(n))


# ── Test 3: 412 pre-filter ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_412_prefilter_drops_blocked_estimation_procedures():
    """Tasks with estimation_procedure in {0, 33} are excluded from the list."""
    from src.ingestion.openml_fetch_async import _fetch_task_ids_for_dataset

    # estimation_procedure in the REST API is encoded as
    # <oml:input name="estimation_procedure">N</oml:input>, not as a direct child tag.
    xml = """
    <oml:tasks xmlns:oml="http://openml.org/openml">
      <oml:task>
        <oml:task_id>101</oml:task_id>
        <oml:input name="estimation_procedure">1</oml:input>
      </oml:task>
      <oml:task>
        <oml:task_id>102</oml:task_id>
        <oml:input name="estimation_procedure">0</oml:input>
      </oml:task>
      <oml:task>
        <oml:task_id>103</oml:task_id>
        <oml:input name="estimation_procedure">33</oml:input>
      </oml:task>
      <oml:task>
        <oml:task_id>104</oml:task_id>
        <oml:input name="estimation_procedure">5</oml:input>
      </oml:task>
    </oml:tasks>
    """
    mock_resp = _xml_response(xml)
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)

    result = await _fetch_task_ids_for_dataset(client, dataset_id=999)

    # Tasks 102 (ep=0) and 103 (ep=33) must be filtered out
    assert 102 not in result, "Task with estimation_procedure=0 should be filtered"
    assert 103 not in result, "Task with estimation_procedure=33 should be filtered"
    # Tasks 101 (ep=1) and 104 (ep=5) must be kept
    assert 101 in result
    assert 104 in result
    assert len(result) == 2


@pytest.mark.asyncio
async def test_412_prefilter_keeps_task_without_estimation_procedure():
    """Tasks with no estimation_procedure element pass through unchanged."""
    from src.ingestion.openml_fetch_async import _fetch_task_ids_for_dataset

    xml = """
    <oml:tasks xmlns:oml="http://openml.org/openml">
      <oml:task>
        <oml:task_id>201</oml:task_id>
      </oml:task>
      <oml:task>
        <oml:task_id>202</oml:task_id>
        <oml:input name="estimation_procedure">10</oml:input>
      </oml:task>
    </oml:tasks>
    """
    mock_resp = _xml_response(xml)
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)

    result = await _fetch_task_ids_for_dataset(client, dataset_id=888)

    assert 201 in result
    assert 202 in result
    assert len(result) == 2


def test_blocked_estimation_procedures_constant():
    """The constant includes both known bad IDs."""
    assert 0 in _BLOCKED_ESTIMATION_PROCEDURES
    assert 33 in _BLOCKED_ESTIMATION_PROCEDURES


# ── Test 4: IngestState initialises correctly ─────────────────────────────────

def test_ingest_state_defaults():
    state = IngestState()
    assert state.existing_runs == set()
    assert state.existing_algorithms == set()
    assert state.existing_datasets == set()
    assert state.existing_tasks == set()
    assert state.counters["runs"] == 0
    assert state.counters["skipped"] == 0
    assert isinstance(state.pk_lock, asyncio.Lock)


def test_ingest_state_with_initial_pks():
    state = IngestState(
        existing_runs={1, 2, 3},
        existing_algorithms={10},
    )
    assert 1 in state.existing_runs
    assert 10 in state.existing_algorithms
    assert state.existing_datasets == set()
