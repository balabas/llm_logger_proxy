from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    "server": {"host": "127.0.0.1", "port": 8081},
    "upstream": {"url": "http://127.0.0.1:8080"},
    "storage": {"path": "trace.llmtrace", "max_mb": 3},
    "defaults": {"session_id": "unassigned", "branch_id": "main"},
}


def load_config(path: str | Path | None) -> dict[str, dict[str, Any]]:
    config = {section: dict(values) for section, values in DEFAULT_CONFIG.items()}
    if path is None:
        return config
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(config_path)
    with config_path.open("rb") as stream:
        loaded = tomllib.load(stream)
    for section, values in loaded.items():
        if not isinstance(values, dict):
            raise ValueError(f"configuration section [{section}] must be a table")
        config.setdefault(section, {}).update(values)
    return config
