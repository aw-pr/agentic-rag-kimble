"""
openml_fetch_async.py — Async OpenML REST fetcher using httpx.

Mirrors the sync API of openml_fetch.py but uses httpx.AsyncClient for
concurrent HTTP calls. Uses stdlib xml.etree.ElementTree for parsing.

Key design decisions:
- Semaphore-bounded concurrency (default 8) via _get_semaphore() helper.
- Module-level flow cache (dict) replaces lru_cache — lru_cache is not
  async-safe under concurrent access.
- Returns the same FetchedDataset / FetchedRun dataclasses as the sync module.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

from src.ingestion.openml_fetch import FetchedDataset, FetchedRun

logger = logging.getLogger(__name__)

_OPENML_BASE = "https://www.openml.org/api/v1/xml"
_OPENML_JSON_BASE = "https://www.openml.org/api/v1/json"
_NS = "http://openml.org/openml"

# OpenML evaluation function names we pull for each Run. The transform layer
# (transform.py:395-417) looks for these keys when building the Run fact node.
_RUN_MEASURE_FUNCTIONS: tuple[str, ...] = (
    "predictive_accuracy",
    "f_measure",
    "area_under_roc_curve",
    "usercpu_time_millis_training",
)

# Estimation procedure IDs that return HTTP 412 (private/auth-required tasks).
# Pre-filtering these at task-list parse time saves one round-trip per skipped task.
_BLOCKED_ESTIMATION_PROCEDURES: frozenset[int] = frozenset({0, 33})

# Module-level flow cache — keyed by flow_id.
_flow_cache: dict[int, dict] = {}

# Module-level description caches to avoid re-fetching within a run.
_dataset_desc_cache: dict[int, str] = {}
_flow_desc_cache: dict[int, str] = {}

# Maximum characters of OpenML free-form description to keep before appending.
_OPENML_DESC_MAX_CHARS = 1000

# Default semaphore concurrency limit.
_DEFAULT_CONCURRENCY = 8

# Module-level semaphore, lazily initialised by _get_semaphore().
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore(limit: int = _DEFAULT_CONCURRENCY) -> asyncio.Semaphore:
    """Return (or create) the module-level semaphore."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(limit)
    return _semaphore


def reset_semaphore(limit: int = _DEFAULT_CONCURRENCY) -> None:
    """Re-initialise the semaphore with a new limit.

    Call before starting an async ingest if you want non-default concurrency.
    """
    global _semaphore
    _semaphore = asyncio.Semaphore(limit)


def _tag(local: str) -> str:
    """Return a Clark-notation tag for the OpenML namespace."""
    return f"{{{_NS}}}{local}"


def _find_text(elem: ET.Element, path: str, default: str = "") -> str:
    """Find a sub-element by tag name and return its text, or default."""
    found = elem.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


async def _get(client: httpx.AsyncClient, url: str) -> Optional[ET.Element]:
    """Fetch a URL with the semaphore, parse XML, return root element or None."""
    sem = _get_semaphore()
    async with sem:
        try:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            return ET.fromstring(response.text)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug("404 for %s", url)
            else:
                logger.warning("HTTP error for %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            return None


async def _get_json(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    """Fetch a JSON URL with the semaphore. Returns the parsed dict or None."""
    sem = _get_semaphore()
    async with sem:
        try:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.debug("404 for %s", url)
            else:
                logger.warning("HTTP error for %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            return None


async def _fetch_evaluations_for_task(
    client: httpx.AsyncClient, task_id: int
) -> dict[int, dict[str, float]]:
    """Bulk-fetch the four Run measures for a single task.

    Returns ``{run_id: {function_name: float_value}}`` covering every run in
    the task. Issues one JSON call per measure function (four total per task,
    each returning up to 10,000 evaluation rows). This replaces the missing
    per-run `/run/{id}` calls that the XML run-list endpoint cannot deliver.
    """
    page_size = 10000

    async def _fetch_one(fn: str) -> tuple[str, list[dict]]:
        """Paginate until the API returns a short page (or empty)."""
        rows_out: list[dict] = []
        offset = 0
        while True:
            url = (
                f"{_OPENML_JSON_BASE}/evaluation/list/function/{fn}"
                f"/task/{task_id}/limit/{page_size}/offset/{offset}"
            )
            payload = await _get_json(client, url)
            if not payload:
                break
            rows = (payload.get("evaluations") or {}).get("evaluation") or []
            if not rows:
                break
            rows_out.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
        return fn, rows_out

    pairs = await asyncio.gather(*[_fetch_one(fn) for fn in _RUN_MEASURE_FUNCTIONS])

    results: dict[int, dict[str, float]] = {}
    for fn, rows in pairs:
        for row in rows:
            try:
                rid = int(row["run_id"])
                val = float(row["value"])
            except (KeyError, ValueError, TypeError):
                continue
            results.setdefault(rid, {})[fn] = val
    return results


# ── Description helpers ───────────────────────────────────────────────────────


def _clean_openml_description(raw: str, max_chars: int = _OPENML_DESC_MAX_CHARS) -> str:
    """Collapse whitespace, strip, and truncate an OpenML free-form description.

    OpenML descriptions often contain newlines, tabs, and long boilerplate.
    We normalise to single spaces and cap at max_chars to keep embedding
    token budgets sane — some descriptions run to multiple kilobytes.
    """
    import re as _re
    text = _re.sub(r"\s+", " ", raw).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]  # break on word boundary
    return text


# ── Public API ────────────────────────────────────────────────────────────────


async def list_dataset_ids_async(
    client: httpx.AsyncClient,
    task_type: str,
    max_datasets: int,
    min_runs: int,
) -> list[int]:
    """Return dataset IDs for supervised classification tasks.

    Fetches the full task list (up to 10000), groups by dataset_id,
    and returns the top ``max_datasets`` by task count — same heuristic
    as the sync version.
    """
    limit = 10000
    url = f"{_OPENML_BASE}/task/list/type/1/limit/{limit}/offset/0"
    root = await _get(client, url)
    if root is None:
        return []

    dataset_task_counts: dict[int, int] = {}
    for task_elem in root.findall(_tag("task")):
        did_text = _find_text(task_elem, _tag("did"))
        if did_text:
            try:
                did = int(did_text)
                dataset_task_counts[did] = dataset_task_counts.get(did, 0) + 1
            except ValueError:
                pass

    if not dataset_task_counts:
        return []

    eligible = sorted(
        dataset_task_counts.keys(),
        key=lambda did: dataset_task_counts[did],
        reverse=True,
    )
    return eligible[:max_datasets]


async def fetch_dataset_async(
    client: httpx.AsyncClient,
    dataset_id: int,
) -> FetchedDataset | None:
    """Fetch dataset metadata and qualities. Returns None on failure."""
    # Fetch both in parallel
    meta_url = f"{_OPENML_BASE}/data/{dataset_id}"
    qual_url = f"{_OPENML_BASE}/data/qualities/{dataset_id}"

    meta_root, qual_root = await asyncio.gather(
        _get(client, meta_url),
        _get(client, qual_url),
    )

    if meta_root is None:
        logger.warning("No metadata for dataset %d", dataset_id)
        return None

    name = _find_text(meta_root, _tag("name"), default=f"dataset_{dataset_id}")

    qualities: dict[str, str] = {}
    if qual_root is not None:
        for q_elem in qual_root.findall(_tag("quality")):
            q_name = _find_text(q_elem, _tag("name"))
            q_value = _find_text(q_elem, _tag("value"))
            if q_name:
                qualities[q_name] = q_value

    return FetchedDataset(
        dataset_id=dataset_id,
        name=name,
        qualities=qualities,
    )


async def _fetch_task_ids_for_dataset(
    client: httpx.AsyncClient,
    dataset_id: int,
) -> list[int]:
    """Return task IDs for a dataset (supervised classification only).

    Pre-filters tasks whose ``estimation_procedure`` is in
    ``_BLOCKED_ESTIMATION_PROCEDURES`` to avoid HTTP 412 round-trips.
    """
    url = (
        f"{_OPENML_BASE}/task/list/type/1"
        f"/data_id/{dataset_id}/limit/100/offset/0"
    )
    root = await _get(client, url)
    if root is None:
        return []

    task_ids: list[int] = []
    skipped_412 = 0
    for task_elem in root.findall(_tag("task")):
        tid_text = _find_text(task_elem, _tag("task_id"))
        if not tid_text:
            continue
        try:
            task_id = int(tid_text)
        except ValueError:
            continue

        # Check estimation_procedure — tasks with blocked IDs return 412.
        # The value is encoded as <oml:input name="estimation_procedure">N</oml:input>
        ep_id: int | None = None
        for inp_elem in task_elem.findall(_tag("input")):
            if inp_elem.get("name") == "estimation_procedure":
                try:
                    ep_id = int(inp_elem.text.strip()) if inp_elem.text else None
                except (ValueError, AttributeError):
                    pass
                break

        if ep_id is not None and ep_id in _BLOCKED_ESTIMATION_PROCEDURES:
            logger.debug(
                "Pre-filtering task %d (dataset %d): "
                "estimation_procedure=%d is blocked",
                task_id, dataset_id, ep_id,
            )
            skipped_412 += 1
            continue

        task_ids.append(task_id)

    if skipped_412:
        logger.info(
            "Dataset %d: pre-filtered %d task(s) with blocked estimation_procedure",
            dataset_id, skipped_412,
        )

    return task_ids


async def _fetch_runs_for_task(
    client: httpx.AsyncClient,
    task_id: int,
    dataset_id: int,
    limit: int,
) -> list[FetchedRun]:
    """Fetch runs for a single task."""
    url = f"{_OPENML_BASE}/run/list/limit/{limit}/offset/0/task/{task_id}"
    root = await _get(client, url)
    if root is None:
        return []

    runs: list[FetchedRun] = []
    for run_elem in root.findall(_tag("run")):
        try:
            run_id = int(_find_text(run_elem, _tag("run_id"), "0"))
            flow_id = int(_find_text(run_elem, _tag("flow_id"), "0"))
            setup_id_text = _find_text(run_elem, _tag("setup_id"), "0")
            setup_id = int(setup_id_text) if setup_id_text else 0

            if run_id == 0 or flow_id == 0:
                continue

            # Evaluations come from a separate bulk JSON endpoint, merged below.
            runs.append(
                FetchedRun(
                    run_id=run_id,
                    setup_id=setup_id,
                    flow_id=flow_id,
                    dataset_id=dataset_id,
                    task_id=task_id,
                    evaluations={},
                )
            )
        except Exception as exc:
            logger.debug("Skipping run element for task %d: %s", task_id, exc)

    # Hydrate measures from the bulk evaluation endpoint. One call per
    # measure function returns up to 10k rows for the whole task at once —
    # cheaper than 171k per-run `/run/{id}` calls.
    if runs:
        eval_map = await _fetch_evaluations_for_task(client, task_id)
        for r in runs:
            r.evaluations = eval_map.get(r.run_id, {})

    return runs


async def fetch_runs_for_dataset_async(
    client: httpx.AsyncClient,
    dataset_id: int,
    task_type: str,
    max_runs: int | None = None,
) -> list[FetchedRun]:
    """Fetch all supervised-classification runs for a dataset.

    Fetches task IDs first, then fans out to per-task run-list calls
    concurrently (bounded by the module semaphore).
    """
    task_ids = await _fetch_task_ids_for_dataset(client, dataset_id)
    if not task_ids:
        return []

    limit = max_runs if max_runs is not None else 10000

    # Fetch all tasks in parallel
    per_task = await asyncio.gather(
        *[
            _fetch_runs_for_task(client, tid, dataset_id, limit)
            for tid in task_ids
        ]
    )

    all_runs: list[FetchedRun] = []
    for task_runs in per_task:
        all_runs.extend(task_runs)
        if max_runs is not None and len(all_runs) >= max_runs:
            all_runs = all_runs[:max_runs]
            break

    return all_runs


async def fetch_flow_async(
    client: httpx.AsyncClient,
    flow_id: int,
) -> dict | None:
    """Fetch flow (algorithm) metadata. Module-level cache avoids re-fetching."""
    if flow_id in _flow_cache:
        return _flow_cache[flow_id]

    url = f"{_OPENML_BASE}/flow/{flow_id}"
    root = await _get(client, url)
    if root is None:
        result = {"flow_id": flow_id, "name": f"unknown_flow_{flow_id}"}
        _flow_cache[flow_id] = result
        return result

    name = _find_text(root, _tag("name"), default=f"unknown_flow_{flow_id}")
    result = {"flow_id": flow_id, "name": name}
    _flow_cache[flow_id] = result

    # Opportunistically cache the description so fetch_flow_description_async
    # can return it without a second HTTP round-trip to the same URL.
    if flow_id not in _flow_desc_cache:
        raw_desc = _find_text(root, _tag("description"), default="")
        _flow_desc_cache[flow_id] = _clean_openml_description(raw_desc) if raw_desc else ""

    return result


async def fetch_dataset_description_async(
    client: httpx.AsyncClient,
    dataset_id: int,
) -> str:
    """Fetch the free-form description from OpenML for a dataset.

    Returns "" on failure or if the description element is absent.
    Results are cached in _dataset_desc_cache to avoid repeated fetches.
    """
    if dataset_id in _dataset_desc_cache:
        return _dataset_desc_cache[dataset_id]

    url = f"{_OPENML_BASE}/data/{dataset_id}"
    root = await _get(client, url)
    if root is None:
        _dataset_desc_cache[dataset_id] = ""
        return ""

    raw = _find_text(root, _tag("description"), default="")
    result = _clean_openml_description(raw) if raw else ""
    _dataset_desc_cache[dataset_id] = result
    return result


async def fetch_flow_description_async(
    client: httpx.AsyncClient,
    flow_id: int,
) -> str:
    """Fetch the free-form description from OpenML for a flow (algorithm).

    Returns "" on failure or if the description element is absent.
    Results are cached in _flow_desc_cache to avoid repeated fetches.
    """
    if flow_id in _flow_desc_cache:
        return _flow_desc_cache[flow_id]

    url = f"{_OPENML_BASE}/flow/{flow_id}"
    root = await _get(client, url)
    if root is None:
        _flow_desc_cache[flow_id] = ""
        return ""

    raw = _find_text(root, _tag("description"), default="")
    result = _clean_openml_description(raw) if raw else ""
    _flow_desc_cache[flow_id] = result
    return result


async def fetch_task_async(
    client: httpx.AsyncClient,
    task_id: int,
) -> dict | None:
    """Fetch task metadata.

    Returns a dict with task_id, task_type, target_feature,
    evaluation_measure — same shape as _get_task_info() in the sync loader.
    """
    url = f"{_OPENML_BASE}/task/{task_id}"
    root = await _get(client, url)
    if root is None:
        return None

    task_elem = root.find(_tag("task"))
    if task_elem is None:
        # Some responses have the task as root
        task_elem = root

    task_type = _find_text(task_elem, _tag("task_type"), "Supervised Classification")

    # target_feature and evaluation_measure are in <oml:input name="..."> elements
    target_feature = ""
    evaluation_measure = ""
    for inp in task_elem.findall(_tag("input")):
        attr_name = inp.get("name", "")
        if attr_name == "target_feature":
            target_feature = inp.text.strip() if inp.text else ""
        elif attr_name == "evaluation_measures":
            evaluation_measure = inp.text.strip() if inp.text else ""

    return {
        "task_id": task_id,
        "task_type": task_type,
        "target_feature": target_feature,
        "evaluation_measure": evaluation_measure,
    }
