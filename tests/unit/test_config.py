"""Unit tests for src/config.py — no network, no DB required."""

from pathlib import Path

from src.config import REPO_ROOT, Config, get_config


def test_default_config_paths_are_under_repo_root():
    cfg = Config()
    assert cfg.kuzu_db_path.is_relative_to(REPO_ROOT), (
        f"kuzu_db_path {cfg.kuzu_db_path} is not under REPO_ROOT {REPO_ROOT}"
    )
    assert cfg.runs_path.is_relative_to(REPO_ROOT), (
        f"runs_path {cfg.runs_path} is not under REPO_ROOT {REPO_ROOT}"
    )


def test_env_override_kuzu_path(monkeypatch, tmp_path):
    override = str(tmp_path / "my_kuzu")
    monkeypatch.setenv("KUZU_DB_PATH", override)
    cfg = get_config()
    assert cfg.kuzu_db_path == Path(override)


def test_env_override_openml_max_datasets(monkeypatch):
    monkeypatch.setenv("OPENML_MAX_DATASETS", "42")
    cfg = get_config()
    assert cfg.openml_max_datasets == 42


def test_openml_max_datasets_default():
    cfg = Config()
    assert cfg.openml_max_datasets == 500


def test_agent_max_tool_calls_default():
    cfg = Config()
    assert cfg.agent_max_tool_calls == 5


def test_embedding_model_default():
    cfg = Config()
    assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"


def test_claude_model_default():
    cfg = Config()
    assert cfg.claude_model == "claude-sonnet-4-6"


def test_eval_judge_model_default():
    cfg = Config()
    assert cfg.eval_judge_model == "claude-haiku-4-5-20251001"


def test_chroma_db_path_removed_from_config():
    """chroma_db_path was dropped in pass 21 — single-store LadybugDB only."""
    cfg = Config()
    assert not hasattr(cfg, "chroma_db_path"), (
        "chroma_db_path should have been removed from Config; ChromaDB is no longer used"
    )


def test_get_config_without_env_vars_returns_defaults(monkeypatch):
    # Ensure none of the override env vars are set
    for var in ("KUZU_DB_PATH", "OPENML_MAX_DATASETS"):
        monkeypatch.delenv(var, raising=False)
    cfg = get_config()
    assert cfg.openml_max_datasets == 500
    assert cfg.kuzu_db_path == REPO_ROOT / "data" / "kuzu_db"
