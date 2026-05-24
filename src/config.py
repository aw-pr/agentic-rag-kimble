import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


@dataclass
class Config:
    # Paths
    kuzu_db_path: Path = REPO_ROOT / "data" / "kuzu_db"
    runs_path: Path = REPO_ROOT / "runs"

    # Ingestion scope
    openml_max_datasets: int = 500
    openml_min_runs_per_dataset: int = 10
    openml_task_type: str = "Supervised Classification"

    # Embeddings
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = 64
    # Compute device for sentence-transformers. "auto" picks the best
    # available: MPS (Apple Metal) → CUDA → CPU. Override to a literal
    # "cpu", "mps", or "cuda" to force a specific backend. Env var:
    # EMBEDDING_DEVICE.
    embedding_device: str = "auto"

    # Async ingestion
    openml_max_concurrent_datasets: int = 8

    # Agent
    claude_model: str = "claude-sonnet-4-6"
    agent_max_tool_calls: int = 15

    # Eval
    eval_judge_model: str = "claude-haiku-4-5-20251001"


def get_config() -> Config:
    cfg = Config()
    if p := os.getenv("KUZU_DB_PATH"):
        cfg.kuzu_db_path = Path(p)
    if n := os.getenv("OPENML_MAX_DATASETS"):
        cfg.openml_max_datasets = int(n)
    if n := os.getenv("OPENML_MAX_CONCURRENT_DATASETS"):
        cfg.openml_max_concurrent_datasets = int(n)
    if d := os.getenv("EMBEDDING_DEVICE"):
        cfg.embedding_device = d
    return cfg
