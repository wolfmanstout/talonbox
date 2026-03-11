from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_DIR_ENV = "MIMIC_CLI_STATE_DIR"


@dataclass(slots=True)
class StatePaths:
    state_dir: Path
    state_path: Path
    log_path: Path


@dataclass(slots=True)
class StateRecord:
    vm: str
    pid: int
    log_path: str
    started_at: str


def get_state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "mimic-cli"


def state_paths(vm: str) -> StatePaths:
    state_dir = get_state_dir()
    return StatePaths(
        state_dir=state_dir,
        state_path=state_dir / f"{vm}.json",
        log_path=state_dir / f"{vm}.log",
    )


def save_state(record: StateRecord) -> None:
    paths = state_paths(record.vm)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.state_path.write_text(json.dumps(asdict(record), indent=2))


def load_state(vm: str) -> StateRecord | None:
    path = state_paths(vm).state_path
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return StateRecord(**data)


def clear_state(vm: str) -> None:
    path = state_paths(vm).state_path
    if path.exists():
        path.unlink()
