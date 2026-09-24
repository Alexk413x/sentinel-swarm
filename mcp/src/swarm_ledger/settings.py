from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROLES = ("oracle", "manager", "lead", "coder")

_DEFAULT_RUNTIME = {
    "oracle": "session",
    "manager": "session",
    "lead": "session",
    "coder": "session",
}
_DEFAULT_MODELS = {
    "oracle": ["fable", "opus"],
    "manager": ["opus"],
    "lead": ["opus", "sonnet"],
    "coder": ["sonnet", "haiku"],
}
_SETTINGS_PATH = Path(".claude") / "sentinel-swarm.local.md"


@dataclass
class RubricSettings:
    target: int = 90
    floor: int = 70
    criterion_floor: int = 5
    disagreement_gap: int = 10
    plateau: int = 2
    regression_tolerance: int = 5


@dataclass
class EscalationSettings:
    rounds: int = 3
    attempts_per_round: int = 3


@dataclass
class Settings:
    tracking: str = "local"
    runtime: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_RUNTIME))
    models: dict[str, list[str]] = field(
        default_factory=lambda: {role: list(models) for role, models in _DEFAULT_MODELS.items()}
    )
    rubric: RubricSettings = field(default_factory=RubricSettings)
    escalation: EscalationSettings = field(default_factory=EscalationSettings)
    test_command: str | None = None
    build_command: str | None = None
    lint_command: str | None = None
    parallelism_cap: int | None = None

    def snapshot(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}
    loaded = yaml.safe_load("\n".join(lines[1:end]))
    return loaded if isinstance(loaded, dict) else {}


def load_settings(repo_root: Path) -> Settings:
    path = repo_root / _SETTINGS_PATH
    data: dict[str, Any] = {}
    if path.is_file():
        data = _frontmatter(path.read_text(encoding="utf-8"))

    defaults = Settings()

    runtime = dict(defaults.runtime)
    runtime.update(data.get("runtime") or {})

    models = {role: list(approved) for role, approved in defaults.models.items()}
    for role, approved in (data.get("models") or {}).items():
        if approved:
            models[role] = list(approved)

    rubric_data = data.get("rubric") or {}
    rubric = RubricSettings(
        target=rubric_data.get("target", defaults.rubric.target),
        floor=rubric_data.get("floor", defaults.rubric.floor),
        criterion_floor=rubric_data.get("criterion_floor", defaults.rubric.criterion_floor),
        disagreement_gap=rubric_data.get("disagreement_gap", defaults.rubric.disagreement_gap),
        plateau=rubric_data.get("plateau", defaults.rubric.plateau),
        regression_tolerance=rubric_data.get(
            "regression_tolerance", defaults.rubric.regression_tolerance
        ),
    )

    escalation_data = data.get("escalation") or {}
    escalation = EscalationSettings(
        rounds=escalation_data.get("rounds", defaults.escalation.rounds),
        attempts_per_round=escalation_data.get(
            "attempts_per_round", defaults.escalation.attempts_per_round
        ),
    )

    return Settings(
        tracking=data.get("tracking") or defaults.tracking,
        runtime=runtime,
        models=models,
        rubric=rubric,
        escalation=escalation,
        test_command=data.get("test_command") or defaults.test_command,
        build_command=data.get("build_command") or defaults.build_command,
        lint_command=data.get("lint_command") or defaults.lint_command,
        parallelism_cap=data.get("parallelism_cap") or defaults.parallelism_cap,
    )
