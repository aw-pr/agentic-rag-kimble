"""
app.py — Streamlit demo UI for agentic-rag-kimble.

Four-panel layout:
  Header (title + DB metrics)
  Query input + Run button
  Tool trace (left 35%) | Answer (right 65%)
  Footer: eval score · run metadata · graph stats

Run via:  streamlit run src/ui/app.py
Or via:   ./run-secure-query.sh streamlit run src/ui/app.py
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# Ensure repo root is importable when launched via `streamlit run src/ui/app.py`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.config import get_config  # noqa: E402  (must follow the sys.path.insert above)

# ---------------------------------------------------------------------------
# Page config — must be the first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Agentic RAG — ML Knowledge Graph",
    page_icon="🔬",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Citation pattern — same regex as orchestrator.py
# ---------------------------------------------------------------------------
_CITATION_RE = re.compile(r"(\[(?:Algorithm|Dataset|Task|Run): ?[^\]]+\])")

EXAMPLE_QUERIES = [
    "Which algorithm families perform best on small, high-dimensional datasets "
    "with class imbalance?",
    "What does the evidence suggest for a tabular classification task with 50k rows "
    "and 40 features?",
    "Compare tree ensemble methods against gradient boosting on datasets with >100 features",
]

# ---------------------------------------------------------------------------
# Pure helpers (no Streamlit state — tested in tests/unit/test_ui_helpers.py)
# ---------------------------------------------------------------------------


def highlight_citations(answer: str) -> str:
    """
    Wrap [Type: value] citations with a yellow highlight span.

    The answer text is HTML-escaped first to prevent XSS from model output.
    The citation spans themselves are then injected as safe HTML.
    """
    # 1. Escape the whole answer to neutralise any HTML in model output
    escaped = html.escape(answer)

    # 2. Re-apply the citation pattern on the escaped text.
    #    Citation strings contain only [A-Za-z0-9: _-] so escaping does not
    #    alter them — but we check for the pattern on the escaped string anyway.
    def _wrap(m: re.Match) -> str:
        citation = m.group(1)
        # Inherit the theme's text colour and add a semi-transparent amber
        # accent (bg + border) so the pill stays legible in light AND dark
        # Streamlit themes. The previous solid pale-yellow fill clashed with
        # light text in dark mode.
        return (
            '<span style="background:rgba(251,191,36,0.18);'
            'color:inherit;'
            'border:1px solid rgba(251,191,36,0.55);'
            'border-radius:4px;padding:1px 6px;'
            f'font-weight:500;white-space:nowrap">{citation}</span>'
        )

    return _CITATION_RE.sub(_wrap, escaped)


def format_stats(stats: dict) -> dict:
    """Return a display-ready dict from raw node-count stats."""
    return {
        "Algorithms": stats.get("algorithm_count", 0),
        "Datasets": stats.get("dataset_count", 0),
        "Tasks": stats.get("task_count", 0),
        "Runs": stats.get("run_count", 0),
    }


# ---------------------------------------------------------------------------
# Cached DB stats (LadybugDB connection opened/closed inside — not picklable)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300)
def load_graph_stats() -> dict:
    """
    Open GraphDB, fetch node counts, close. Returns a plain dict (picklable).
    Cached for 5 minutes.
    """
    from src.graph.db import GraphDB

    config = get_config()
    try:
        with GraphDB(config) as db:
            run_count = db.node_count("Run")
            algorithm_count = db.node_count("Algorithm")
            dataset_count = db.node_count("Dataset")
            task_count = db.node_count("Task")
        return {
            "run_count": run_count,
            "algorithm_count": algorithm_count,
            "dataset_count": dataset_count,
            "task_count": task_count,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "run_count": 0,
            "algorithm_count": 0,
            "dataset_count": 0,
            "task_count": 0,
            "error": str(exc),
        }


@st.cache_data(ttl=300)
def load_dimension_data() -> dict:
    """
    Fetch all data needed by the Dimensions page in one DB session.
    Returns a dict of plain Python structures (picklable).
    """
    from src.graph.db import GraphDB

    config = get_config()
    out: dict = {"error": None}
    try:
        with GraphDB(config) as db:
            out["family_dist"] = db.execute(
                "MATCH (a:Algorithm) "
                "RETURN a.family AS family, count(a) AS algorithms "
                "ORDER BY algorithms DESC"
            )
            out["family_runs"] = db.execute(
                "MATCH (a:Algorithm)<-[:USED_ALGORITHM]-(r:Run) "
                "RETURN a.family AS family, count(r) AS runs "
                "ORDER BY runs DESC"
            )
            out["other_flows"] = db.execute(
                "MATCH (a:Algorithm) WHERE a.family = 'other' "
                "OPTIONAL MATCH (a)<-[:USED_ALGORITHM]-(r:Run) "
                "RETURN a.flow_id AS flow_id, a.name AS name, "
                "a.display_name AS display_name, count(r) AS runs "
                "ORDER BY runs DESC LIMIT 200"
            )
            out["task_types"] = db.execute(
                "MATCH (t:Task) RETURN t.task_type AS task_type, "
                "count(t) AS tasks ORDER BY tasks DESC"
            )
            out["datasets_raw"] = db.execute(
                "MATCH (d:Dataset) RETURN d.dataset_id AS dataset_id, "
                "d.name AS name, d.n_rows AS n_rows, d.n_features AS n_features, "
                "d.n_classes AS n_classes, d.imbalance_ratio AS imbalance_ratio, "
                "d.size_bucket AS size_bucket, d.dim_bucket AS dim_bucket, "
                "d.imbalance_bucket AS imbalance_bucket"
            )
            out["paradigm_dist"] = db.execute(
                "MATCH (a:Algorithm) WHERE a.paradigm IS NOT NULL "
                "RETURN a.paradigm AS paradigm, count(a) AS algorithms "
                "ORDER BY algorithms DESC"
            )
            out["cost_dist"] = db.execute(
                "MATCH (a:Algorithm) WHERE a.training_cost_class IS NOT NULL "
                "RETURN a.training_cost_class AS training_cost_class, count(a) AS algorithms "
                "ORDER BY algorithms DESC"
            )
            out["sample_descriptions"] = db.execute(
                "MATCH (a:Algorithm) "
                "RETURN a.family AS family, a.display_name AS display_name, "
                "a.description AS description LIMIT 400"
            )
            out["date_dist"] = db.execute(
                "MATCH (r:Run)-[:RUN_ON_DATE]->(dt:Date) "
                "RETURN dt.year AS year, count(r) AS runs "
                "ORDER BY year"
            )
            out["date_quarter_dist"] = db.execute(
                "MATCH (r:Run)-[:RUN_ON_DATE]->(dt:Date) "
                "RETURN dt.year AS year, dt.quarter AS quarter, count(r) AS runs "
                "ORDER BY year, quarter"
            )
            out["algorithm_families"] = db.execute(
                "MATCH (f:AlgorithmFamily) "
                "RETURN f.display_name AS display_name, "
                "f.family_name AS family_name, "
                "f.paradigm AS paradigm, "
                "f.interpretability AS interpretability, "
                "f.typical_use_case AS typical_use_case "
                "ORDER BY f.display_name"
            )
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def load_latest_eval() -> dict | None:
    """
    Read the most-recently modified JSON file in runs/, return as dict.
    Returns None if no eval files exist.
    """
    config = get_config()
    runs_dir: Path = config.runs_path
    json_files = list(runs_dir.glob("*.json"))
    if not json_files:
        return None
    latest = max(json_files, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(latest.read_text())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------


def _init_session_state() -> None:
    if "query_history" not in st.session_state:
        st.session_state.query_history = []  # list of last-5 query strings
    if "last_response" not in st.session_state:
        st.session_state.last_response = None
    if "last_elapsed" not in st.session_state:
        st.session_state.last_elapsed = None
    if "query_input" not in st.session_state:
        st.session_state.query_input = ""


# ---------------------------------------------------------------------------
# Sidebar — query history
# ---------------------------------------------------------------------------


def _render_sidebar() -> str:
    """Render sidebar and return the selected page name."""
    with st.sidebar:
        page = st.radio(
            "Page",
            ["Query", "Schema", "Dimensions"],
            key="page_select",
            label_visibility="collapsed",
        )
        st.divider()
        st.header("Query History")
        history: list[str] = st.session_state.query_history
        if not history:
            st.caption("No queries yet.")
        else:
            for i, q in enumerate(reversed(history), start=1):
                st.markdown(f"**{i}.** {q[:80]}{'…' if len(q) > 80 else ''}")
        if history:
            if st.button("Clear history", key="clear_history"):
                st.session_state.query_history = []
                st.rerun()

        st.divider()
        st.caption(
            "Auth is automatic via your Claude Code OAuth session (no 1Password injection)."
        )
    return page


# ---------------------------------------------------------------------------
# Header panel
# ---------------------------------------------------------------------------


def _render_header(stats: dict) -> None:
    st.title("🔬 Agentic RAG — ML Experiment Knowledge Graph")
    st.caption(
        "Kimball-structured property graph · OpenML data · "
        "Claude agent with Cypher, semantic search, and aggregation tools"
    )

    if stats.get("error"):
        st.warning(
            f"Could not connect to graph database: {stats['error']}  \n"
            "Run `./scripts/ingest.sh` to populate the knowledge graph."
        )
    elif stats["run_count"] == 0:
        st.warning(
            "Knowledge graph is empty. "
            "Run `./scripts/ingest.sh` to populate the knowledge graph."
        )
    else:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Algorithms", stats["algorithm_count"])
        col2.metric("Datasets", stats["dataset_count"])
        col3.metric("Tasks", stats["task_count"])
        col4.metric("Runs", f"{stats['run_count']:,}")


# ---------------------------------------------------------------------------
# Query panel
# ---------------------------------------------------------------------------


def _render_query_panel() -> tuple[str, bool]:
    """
    Render query text area + example buttons + Run button.
    Returns (query_text, run_clicked).
    """
    st.subheader("Query")

    # Example query quick-fill buttons
    ecol1, ecol2, ecol3 = st.columns(3)
    fill_query = ""
    if ecol1.button("Example 1", key="ex1", help=EXAMPLE_QUERIES[0]):
        fill_query = EXAMPLE_QUERIES[0]
    if ecol2.button("Example 2", key="ex2", help=EXAMPLE_QUERIES[1]):
        fill_query = EXAMPLE_QUERIES[1]
    if ecol3.button("Example 3", key="ex3", help=EXAMPLE_QUERIES[2]):
        fill_query = EXAMPLE_QUERIES[2]

    if fill_query:
        st.session_state.query_input = fill_query

    query_text = st.text_area(
        "Enter your query",
        value=st.session_state.query_input,
        height=80,
        key="query_text_area",
        label_visibility="collapsed",
        placeholder="e.g. Which algorithms work best on high-dimensional datasets?",
    )

    run_clicked = st.button("Run", type="primary", key="run_btn")
    return query_text.strip(), run_clicked


# ---------------------------------------------------------------------------
# Tool trace + answer panels
# ---------------------------------------------------------------------------


def _render_tool_trace(tool_calls: list[dict], total: int) -> None:
    st.subheader("Tool Trace")
    if not tool_calls:
        st.caption("No tool calls.")
        return
    for i, call in enumerate(tool_calls):
        name = call.get("name", "unknown")
        with st.expander(f"🔧 {name} ({i + 1}/{total})", expanded=False):
            st.json(call.get("input", {}))
            # Prefer the actual tool result (pass-27); fall back to result_preview
            output = call.get("result") or call.get("result_preview", "")
            if output:
                st.caption("Result:")
                st.code(output, language="json")


def _render_answer(answer: str, total_tool_calls: int, elapsed: float) -> None:
    st.subheader("Answer")
    if not answer:
        st.caption("No answer yet.")
        return

    highlighted = highlight_citations(answer)
    st.markdown(highlighted, unsafe_allow_html=True)
    st.caption(
        f"{total_tool_calls} tool call{'s' if total_tool_calls != 1 else ''} · {elapsed:.1f}s"
    )


# ---------------------------------------------------------------------------
# Footer panel
# ---------------------------------------------------------------------------


def _render_footer(
    stats: dict,
    last_response,  # AgentResponse | None
    last_elapsed: float | None,
) -> None:
    st.divider()
    fcol1, fcol2, fcol3 = st.columns(3)

    # Col 1 — Eval score
    with fcol1:
        st.markdown("**Eval Score**")
        eval_data = load_latest_eval()
        if eval_data is None:
            st.caption("No eval results found.")
            st.caption("Run: `python -m src.eval.metrics`")
        else:
            recall = eval_data.get("recall_at_10")
            judge = eval_data.get("judge_score")
            if recall is not None:
                st.metric("Recall@10", f"{recall:.3f}")
            if isinstance(judge, dict):
                overall = judge.get("mean_overall")
                if overall is not None:
                    n = judge.get("samples_scored")
                    label = f"Judge overall (n={n})" if n else "Judge overall"
                    st.metric(label, f"{overall:.2f}")
            elif judge is not None:
                st.metric("Judge score", f"{judge:.2f}")
            if recall is None and judge is None:
                st.caption("Eval file found but no scores present.")

    # Col 2 — Run metadata
    with fcol2:
        st.markdown("**Last Run**")
        if last_response is None:
            st.caption("No query run yet.")
        else:
            st.caption(f"Time: {datetime.now().strftime('%H:%M:%S')}")
            st.caption(f"Model: `{get_config().claude_model}`")
            st.caption(f"Tool calls: {last_response.total_tool_calls}")
            st.caption(f"Citations: {len(last_response.citations)}")
            if last_elapsed is not None:
                st.caption(f"Elapsed: {last_elapsed:.1f}s")

    # Col 3 — Graph stats
    with fcol3:
        st.markdown("**Graph Stats**")
        if stats.get("error"):
            st.caption(f"DB error: {stats['error']}")
        else:
            for label, count in format_stats(stats).items():
                st.caption(f"{label}: {count:,}")


# ---------------------------------------------------------------------------
# Dimensions explorer page
# ---------------------------------------------------------------------------


def _bucket_rows(n_rows: int | None) -> str:
    if n_rows is None:
        return "unknown"
    if n_rows < 1000:
        return "small (<1k)"
    if n_rows <= 100_000:
        return "medium (1k–100k)"
    return "large (>100k)"


def _bucket_features(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n < 20:
        return "low (<20)"
    if n <= 100:
        return "medium (20–100)"
    return "high (>100)"


def _bucket_imbalance(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio < 1.5:
        return "balanced (<1.5)"
    if ratio <= 5.0:
        return "moderate (1.5–5)"
    return "severe (>5)"


def _render_dimensions_page(stats: dict) -> None:
    import pandas as pd

    st.title("📐 Dimension Explorer")
    st.caption(
        "Inspect the Kimball dimension layer: Algorithm families, Dataset shape "
        "buckets, Task types, and synthesised descriptions used for embedding."
    )

    if stats.get("error") or stats.get("run_count", 0) == 0:
        st.warning("Graph is empty or unreachable. Populate it via `./scripts/ingest.sh`.")
        return

    data = load_dimension_data()
    if data.get("error"):
        st.error(f"Failed to load dimension data: {data['error']}")
        return

    # ── Algorithm tab ──────────────────────────────────────────────────────
    tab_alg, tab_ds, tab_task, tab_desc, tab_date, tab_family = st.tabs(
        ["Algorithm", "Dataset", "Task", "Sample descriptions", "Date", "Family detail"]
    )

    with tab_alg:
        st.subheader("Algorithm family distribution")
        st.caption(
            "`family` is a synthesised dimension attribute (rule-based, see "
            "`derive_algorithm_family` in `src/ingestion/transform.py`)."
        )

        fam_df = pd.DataFrame(data["family_dist"])
        run_df = pd.DataFrame(data["family_runs"])
        if not fam_df.empty and not run_df.empty:
            merged = fam_df.merge(run_df, on="family", how="outer").fillna(0)
            merged["algorithms"] = merged["algorithms"].astype(int)
            merged["runs"] = merged["runs"].astype(int)
            merged = merged.sort_values("algorithms", ascending=False)

            c1, c2 = st.columns([45, 55])
            with c1:
                st.dataframe(merged, use_container_width=True, hide_index=True)
            with c2:
                st.bar_chart(merged.set_index("family")["algorithms"])

            other_pct = (
                merged.loc[merged["family"] == "other", "algorithms"].sum()
                / max(merged["algorithms"].sum(), 1)
                * 100
            )
            if other_pct > 0:
                st.caption(
                    f"`other` accounts for **{other_pct:.1f}%** of distinct algorithms — "
                    "candidates for `_FAMILY_RULES` extension below."
                )

        st.divider()
        st.subheader("By paradigm and training cost class")
        st.caption(
            "Derived Kimball dimension attributes: `paradigm` and `training_cost_class`. "
            "Columns are NULL on a pre-backfill DB — run "
            "`scripts/backfill-dimension-attributes.sh` "
            "or re-ingest to populate."
        )

        paradigm_df = pd.DataFrame(data.get("paradigm_dist") or [])
        cost_df = pd.DataFrame(data.get("cost_dist") or [])

        if not paradigm_df.empty and paradigm_df["algorithms"].sum() > 0:
            pc1, pc2 = st.columns(2)
            with pc1:
                st.markdown("**By paradigm**")
                st.bar_chart(paradigm_df.set_index("paradigm")["algorithms"])
            with pc2:
                if not cost_df.empty and cost_df["algorithms"].sum() > 0:
                    st.markdown("**By training cost class**")
                    st.bar_chart(cost_df.set_index("training_cost_class")["algorithms"])
                else:
                    st.caption("No `training_cost_class` values stored yet.")
        else:
            st.caption(
                "No `paradigm` values stored yet — all NULL. "
                "Run the backfill script or re-ingest to populate."
            )

        st.divider()
        st.subheader("Inside `family = 'other'` (top 200 by run count)")
        other_df = pd.DataFrame(data["other_flows"])
        if other_df.empty:
            st.caption("No algorithms classified as `other`.")
        else:
            other_df["runs"] = other_df["runs"].astype(int)
            search = st.text_input(
                "Filter by name substring (case-insensitive)",
                key="other_filter",
            ).strip().lower()
            view = other_df
            if search:
                mask = (
                    view["name"].fillna("").str.lower().str.contains(search)
                    | view["display_name"].fillna("").str.lower().str.contains(search)
                )
                view = view[mask]
            st.dataframe(view, use_container_width=True, hide_index=True)
            st.caption(
                f"{len(view):,} of {len(other_df):,} rows shown. "
                "Use this list to seed new `_FAMILY_RULES` entries."
            )

    # ── Dataset tab ────────────────────────────────────────────────────────
    with tab_ds:
        st.subheader("Dataset shape buckets")
        st.caption(
            "Stored bucket columns (size_bucket, dim_bucket, imbalance_bucket) are "
            "used when available; Python fallback helpers apply when columns are NULL "
            "(pre-backfill DB)."
        )

        ds_df = pd.DataFrame(data["datasets_raw"])
        if ds_df.empty:
            st.caption("No datasets ingested.")
        else:
            # Prefer stored columns; fall back to Python helpers for NULL values.
            def _resolve_bucket(row, stored_col, fallback_fn, raw_col):
                v = row.get(stored_col)
                if v is not None and str(v).strip():
                    return v
                return fallback_fn(row.get(raw_col))

            ds_df["rows_bucket"] = ds_df.apply(
                lambda r: _resolve_bucket(r, "size_bucket", _bucket_rows, "n_rows"), axis=1
            )
            ds_df["features_bucket"] = ds_df.apply(
                lambda r: _resolve_bucket(r, "dim_bucket", _bucket_features, "n_features"), axis=1
            )
            ds_df["imbalance_bucket_col"] = ds_df.apply(
                lambda r: _resolve_bucket(
                    r, "imbalance_bucket", _bucket_imbalance, "imbalance_ratio"
                ),
                axis=1,
            )

            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown("**By row count**")
                st.bar_chart(ds_df["rows_bucket"].value_counts())
            with c2:
                st.markdown("**By feature count**")
                st.bar_chart(ds_df["features_bucket"].value_counts())
            with c3:
                st.markdown("**By class imbalance**")
                st.bar_chart(ds_df["imbalance_bucket_col"].value_counts())

            st.divider()
            st.markdown("**Raw dataset table**")
            st.dataframe(
                ds_df[[
                    "dataset_id", "name", "n_rows", "n_features",
                    "n_classes", "imbalance_ratio",
                    "rows_bucket", "features_bucket", "imbalance_bucket_col",
                ]],
                use_container_width=True,
                hide_index=True,
            )

    # ── Task tab ───────────────────────────────────────────────────────────
    with tab_task:
        st.subheader("Task types")
        task_df = pd.DataFrame(data["task_types"])
        if task_df.empty:
            st.caption("No tasks ingested.")
        else:
            task_df["tasks"] = task_df["tasks"].astype(int)
            c1, c2 = st.columns([45, 55])
            with c1:
                st.dataframe(task_df, use_container_width=True, hide_index=True)
            with c2:
                st.bar_chart(task_df.set_index("task_type")["tasks"])

    # ── Date tab ──────────────────────────────────────────────────────────
    with tab_date:
        st.subheader("Run volume over time")
        st.caption(
            "Populated from the `upload_time` field on each OpenML run via the "
            "`RUN_ON_DATE` relationship. Run `scripts/backfill-date-dimension.sh` "
            "on an existing DB to populate this dimension."
        )
        date_df = pd.DataFrame(data.get("date_dist") or [])
        if date_df.empty:
            st.caption(
                "No Date dimension data found. Either the DB is pre-backfill or "
                "no runs have an `upload_date`. Run "
                "`scripts/backfill-date-dimension.sh` to populate."
            )
        else:
            date_df["year"] = date_df["year"].astype(int)
            date_df["runs"] = date_df["runs"].astype(int)
            st.bar_chart(date_df.set_index("year")["runs"])

            quarter_df = pd.DataFrame(data.get("date_quarter_dist") or [])
            if not quarter_df.empty:
                st.divider()
                st.markdown("**Year / quarter breakdown**")
                quarter_df["runs"] = quarter_df["runs"].astype(int)
                st.dataframe(quarter_df, use_container_width=True, hide_index=True)

    # ── Family detail tab ──────────────────────────────────────────────────
    with tab_family:
        st.subheader("AlgorithmFamily dimension")
        st.caption(
            "Curated outrigger sub-dimension (Stage 3). Each row represents one "
            "algorithm family and carries a description that is embedded for "
            "`semantic_search(entity_type='AlgorithmFamily', ...)`. "
            "Populate via `./scripts/backfill-algorithm-families.sh` on an existing DB, "
            "or run a fresh ingestion."
        )
        fam_detail_rows = data.get("algorithm_families") or []
        fam_detail_df = pd.DataFrame(fam_detail_rows)
        if fam_detail_df.empty:
            st.caption(
                "No AlgorithmFamily nodes found. Run "
                "`./scripts/backfill-algorithm-families.sh` to populate this dimension, "
                "or re-ingest from scratch."
            )
        else:
            cols_order = [
                c for c in ["display_name", "family_name", "paradigm",
                             "interpretability", "typical_use_case"]
                if c in fam_detail_df.columns
            ]
            st.dataframe(
                fam_detail_df[cols_order],
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"{len(fam_detail_df)} family rows loaded.")

    # ── Sample descriptions tab ────────────────────────────────────────────
    with tab_desc:
        st.subheader("Synthesised algorithm descriptions")
        st.caption(
            "These strings feed the BAAI/bge-small-en-v1.5 embedding for "
            "`semantic_search` on Algorithm nodes. Edit "
            "`synthesise_description` in `transform.py` to change them."
        )
        desc_df = pd.DataFrame(data["sample_descriptions"])
        if desc_df.empty:
            st.caption("No descriptions found.")
        else:
            families = sorted(desc_df["family"].dropna().unique().tolist())
            chosen = st.selectbox("Filter by family", ["(all)"] + families, key="desc_fam")
            view = desc_df if chosen == "(all)" else desc_df[desc_df["family"] == chosen]
            st.dataframe(view, use_container_width=True, hide_index=True)
            st.caption(f"Showing {len(view):,} algorithm rows.")


# ---------------------------------------------------------------------------
# Schema page — Kimball star + agent trace overlay
# ---------------------------------------------------------------------------


_DIM_LABELS = {"Algorithm", "Dataset", "Task", "Date", "AlgorithmFamily"}
_FACT_LABEL = "Run"
_MEASURES = ("accuracy", "f1", "auc", "runtime_sec", "memory_mb")

# Map (set of touched node labels) to relationship edges that should light up.
_REL_EDGES = [
    ("Run", "Algorithm", "USED_ALGORITHM"),
    ("Run", "Dataset", "ON_DATASET"),
    ("Run", "Task", "FOR_TASK"),
    ("Dataset", "Task", "PART_OF_TASK"),
    ("Run", "Date", "RUN_ON_DATE"),
    ("Algorithm", "AlgorithmFamily", "BELONGS_TO_FAMILY"),
]


def _extract_trace(tool_calls: list[dict]) -> dict:
    """Infer which nodes / edges / measures each tool call touched.

    Returns:
      {
        "nodes":    set[str]        — labels touched across all tool calls
        "edges":    set[tuple]      — (a, b, rel) triples touched
        "measures": set[str]        — Run measure names referenced
        "steps":    list[dict]      — per-call breakdown for display
      }
    """
    nodes: set[str] = set()
    edges: set[tuple] = set()
    measures: set[str] = set()
    steps: list[dict] = []

    label_pat = re.compile(r":\s*(Algorithm|AlgorithmFamily|Dataset|Task|Run|Date)\b")
    measure_pat = re.compile(r"\.(accuracy|f1|auc|runtime_sec|memory_mb)\b")

    for call in tool_calls:
        name = call.get("name", "")
        inp = call.get("input", {}) or {}
        step_nodes: set[str] = set()
        step_measures: set[str] = set()

        if name == "semantic_search":
            etype = inp.get("entity_type")
            if isinstance(etype, str) and etype in _DIM_LABELS:
                step_nodes.add(etype)

        elif name == "aggregate_measures":
            # group_by like "algorithm.family" → Algorithm dimension
            group_by = (inp.get("group_by") or "").lower()
            if group_by.startswith("algorithm"):
                step_nodes.add("Algorithm")
            elif group_by.startswith("dataset"):
                step_nodes.add("Dataset")
            elif group_by.startswith("task"):
                step_nodes.add("Task")
            elif group_by.startswith("date"):
                step_nodes.add("Date")
            # Aggregating any measure touches the Run fact
            step_nodes.add("Run")
            m = inp.get("measure")
            if isinstance(m, str) and m in _MEASURES:
                step_measures.add(m)
            # filter_cypher may pull in extra labels
            fc = inp.get("filter_cypher") or ""
            if isinstance(fc, str):
                for lbl in label_pat.findall(fc):
                    step_nodes.add(lbl)

        elif name == "graph_query":
            cypher = inp.get("cypher") or inp.get("query") or ""
            if isinstance(cypher, str):
                for lbl in label_pat.findall(cypher):
                    step_nodes.add(lbl)
                for mname in measure_pat.findall(cypher):
                    step_measures.add(mname)

        # Derive edges from the labels touched in this step
        step_edges: set[tuple] = set()
        for a, b, rel in _REL_EDGES:
            if a in step_nodes and b in step_nodes:
                step_edges.add((a, b, rel))

        nodes |= step_nodes
        edges |= step_edges
        measures |= step_measures
        steps.append({
            "tool": name,
            "nodes": sorted(step_nodes),
            "edges": sorted(f"{a}-[{r}]->{b}" for a, b, r in step_edges),
            "measures": sorted(step_measures),
            "input": inp,
        })

    return {"nodes": nodes, "edges": edges, "measures": measures, "steps": steps}


def _build_schema_mermaid(
    stats: dict,
    touched_nodes: set[str],
    touched_edges: set[tuple],
    touched_measures: set[str],
) -> str:
    """Build a Mermaid flowchart of the Kimball star with optional highlights."""

    def fmt_n(key: str) -> str:
        n = stats.get(key, 0) or 0
        return f"{n:,}"

    # Bold any touched measures inside the Run node label
    measure_parts = []
    for m in _MEASURES:
        if m in touched_measures:
            measure_parts.append(f"<b>{m}</b>")
        else:
            measure_parts.append(m)
    measures_line = " · ".join(measure_parts)

    run_label = (
        f"<b>Run</b> <i>(fact)</i><br/>"
        f"{fmt_n('run_count')} rows<br/>"
        f"<small>{measures_line}</small>"
    )
    alg_label = (
        f"<b>Algorithm</b> <i>(dim)</i><br/>"
        f"{fmt_n('algorithm_count')} rows<br/>"
        f"<small>family · name · description</small>"
    )
    ds_label = (
        f"<b>Dataset</b> <i>(dim)</i><br/>"
        f"{fmt_n('dataset_count')} rows<br/>"
        f"<small>n_rows · n_features · imbalance_ratio</small>"
    )
    task_label = (
        f"<b>Task</b> <i>(dim)</i><br/>"
        f"{fmt_n('task_count')} rows<br/>"
        f"<small>task_type · target_feature · evaluation_measure</small>"
    )
    date_label = (
        "<b>Date</b> <i>(dim)</i><br/>"
        "<small>year · quarter · month · day_of_week</small>"
    )
    family_label = (
        "<b>AlgorithmFamily</b> <i>(outrigger)</i><br/>"
        "<small>paradigm · interpretability · description</small>"
    )

    def edge(a: str, b: str, rel: str) -> str:
        if (a, b, rel) in touched_edges:
            # Thick highlighted edge using `==>` syntax
            return f"    {a} ==>|<b>{rel}</b>| {b}"
        return f"    {a} -->|{rel}| {b}"

    lines = [
        "flowchart LR",
        "    classDef dim fill:#e8f0fe,stroke:#4285f4,stroke-width:1px,color:#1a1a1a;",
        "    classDef outrigger fill:#e6f4ea,stroke:#34a853,stroke-width:1px,color:#1a1a1a;",
        "    classDef fact fill:#fef7e0,stroke:#fbbc04,stroke-width:2px,color:#1a1a1a;",
        "    classDef touched fill:#fce8e6,stroke:#ea4335,stroke-width:3px,color:#1a1a1a;",
        f'    Algorithm["{alg_label}"]',
        f'    AlgorithmFamily["{family_label}"]',
        f'    Dataset["{ds_label}"]',
        f'    Task["{task_label}"]',
        f'    Date["{date_label}"]',
        f'    Run["{run_label}"]',
        edge("Run", "Algorithm", "USED_ALGORITHM"),
        edge("Run", "Dataset", "ON_DATASET"),
        edge("Run", "Task", "FOR_TASK"),
        edge("Dataset", "Task", "PART_OF_TASK"),
        edge("Run", "Date", "RUN_ON_DATE"),
        edge("Algorithm", "AlgorithmFamily", "BELONGS_TO_FAMILY"),
        "    class Algorithm,Dataset,Task,Date dim",
        "    class AlgorithmFamily outrigger",
        "    class Run fact",
    ]

    if touched_nodes:
        # `class` reassignment lets `touched` win over `dim`/`fact`/`outrigger`.
        touched_in_diagram = [n for n in touched_nodes if n in _DIM_LABELS | {_FACT_LABEL}]
        if touched_in_diagram:
            lines.append("    class " + ",".join(touched_in_diagram) + " touched")

    return "\n".join(lines)


def _render_mermaid(diagram: str, height: int = 480) -> None:
    """Render a Mermaid diagram inside Streamlit via an HTML component."""
    import streamlit.components.v1 as components

    html_doc = f"""
    <html>
      <head>
        <script type="module">
          import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
          mermaid.initialize({{ startOnLoad: true, securityLevel: 'loose', theme: 'default' }});
        </script>
        <style>
          body {{ margin: 0; font-family: -apple-system, system-ui, sans-serif; }}
          .mermaid {{ display: flex; justify-content: center; }}
        </style>
      </head>
      <body>
        <div class="mermaid">
{diagram}
        </div>
      </body>
    </html>
    """
    components.html(html_doc, height=height, scrolling=False)


def _render_schema_page(stats: dict) -> None:
    st.title("⭐ Kimball schema · live")
    st.caption(
        "The dimensional model the agent navigates. Last query's path is "
        "overlaid in red — nodes touched, edges traversed, measures aggregated."
    )

    if stats.get("error") or stats.get("run_count", 0) == 0:
        st.warning("Graph is empty or unreachable. Populate it via `./scripts/ingest.sh`.")
        return

    resp = st.session_state.get("last_response")
    trace = _extract_trace(resp.tool_calls) if resp else {
        "nodes": set(), "edges": set(), "measures": set(), "steps": [],
    }

    diagram = _build_schema_mermaid(
        stats, trace["nodes"], trace["edges"], trace["measures"]
    )
    _render_mermaid(diagram, height=460)

    st.divider()

    left, right = st.columns([55, 45])

    with left:
        st.subheader("Last query trace")
        if not resp:
            st.caption(
                "Run a query on the **Query** page, then come back here to see "
                "which dimensions and measures the agent touched."
            )
        else:
            for i, step in enumerate(trace["steps"], start=1):
                with st.expander(
                    f"Tool {i}: `{step['tool']}` → "
                    f"{', '.join(step['nodes']) or '(no labels detected)'}",
                    expanded=(i == 1),
                ):
                    if step["nodes"]:
                        st.markdown("**Nodes:** " + ", ".join(step["nodes"]))
                    if step["edges"]:
                        st.markdown("**Edges:** " + ", ".join(step["edges"]))
                    if step["measures"]:
                        st.markdown(
                            "**Measures:** " + ", ".join(f"`{m}`" for m in step["measures"])
                        )
                    st.caption("Input")
                    st.json(step["input"])

    with right:
        st.subheader("Legend")
        st.markdown(
            "- 🟡 **Run** — fact node, holds the measures\n"
            "- 🔵 **Algorithm / Dataset / Task / Date** — dimension nodes\n"
            "- 🟢 **AlgorithmFamily** — outrigger sub-dimension (snowflake)\n"
            "- 🔴 Highlighted node / thick edge — touched by the last query\n"
            "- **Bold** measure inside Run — aggregated by the last query"
        )
        st.divider()
        st.subheader("Summary")
        if resp:
            st.metric("Tool calls", resp.total_tool_calls)
            st.metric("Dimensions touched", len(trace["nodes"] & _DIM_LABELS))
            st.metric("Measures aggregated", len(trace["measures"]))
        else:
            st.caption("No query run yet this session.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _init_session_state()
    page = _render_sidebar()

    stats = load_graph_stats()

    if page == "Schema":
        _render_schema_page(stats)
        return

    if page == "Dimensions":
        _render_dimensions_page(stats)
        return

    _render_header(stats)

    st.divider()
    query_text, run_clicked = _render_query_panel()

    st.divider()
    left_col, right_col = st.columns([35, 65])

    if run_clicked and query_text:
        # Update history (keep last 5, deduplicate leading entry)
        history: list[str] = st.session_state.query_history
        if not history or history[-1] != query_text:
            history.append(query_text)
            st.session_state.query_history = history[-5:]

        try:
            from src.agent.orchestrator import run_query
            from src.config import get_config as _get_config

            cfg = _get_config()
            t0 = time.monotonic()

            with st.spinner("Agent thinking..."):
                response = run_query(query_text, cfg)

            elapsed = time.monotonic() - t0
            st.session_state.last_response = response
            st.session_state.last_elapsed = elapsed

        except RuntimeError as exc:
            err_msg = str(exc)
            st.error(f"Authentication failed — {err_msg}")
            with st.expander("How to fix"):
                st.markdown(
                    "Claude Agent SDK auth uses your Claude Code OAuth session. "
                    "Sign in to Claude Code, then re-run the app:"
                )
                st.code("streamlit run src/ui/app.py", language="bash")
            st.session_state.last_response = None
            st.session_state.last_elapsed = None

        except Exception as exc:  # noqa: BLE001
            st.error(f"Query failed: {exc}")
            with st.expander("Details"):
                st.code(type(exc).__name__, language="text")
            st.session_state.last_response = None
            st.session_state.last_elapsed = None

    # Render tool trace + answer from session state (persists across reruns)
    resp = st.session_state.last_response
    elapsed_val: float = st.session_state.last_elapsed or 0.0

    with left_col:
        _render_tool_trace(
            resp.tool_calls if resp else [],
            resp.total_tool_calls if resp else 0,
        )

    with right_col:
        _render_answer(
            resp.answer if resp else "",
            resp.total_tool_calls if resp else 0,
            elapsed_val,
        )

    _render_footer(stats, resp, st.session_state.last_elapsed)


if __name__ == "__main__":
    main()
