from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import frontmatter

ROLES = ("oracle", "manager", "lead", "coder", "driver")

_DEFAULT_RUNTIME = {
    "oracle": "session",
    "manager": "session",
    "lead": "session",
    "coder": "session",
    "driver": "session",
}
_DEFAULT_MODELS = {
    "oracle": ["opus", "fable"],
    "manager": ["opus"],
    "lead": ["opus", "sonnet"],
    "coder": ["sonnet", "haiku"],
    # The Driver runs on Sonnet by default, as cartographer's map-driver does.
    "driver": ["sonnet", "opus"],
}
DEFAULT_EFFORT = {role: "medium" for role in _DEFAULT_MODELS}
_SETTINGS_PATH = Path(".claude") / "sentinel-swarm.local.md"
NOTIFY_CHANNELS = ("os", "push")
DEFAULT_MAX_WORKERS = 8
PROFILE_KEYS = ("test_command", "build_command", "lint_command")


def normalize_budget(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        minutes = int(str(raw).strip())
    except ValueError:
        return None
    return minutes if minutes > 0 else None


def normalize_max_workers(raw: object) -> int:
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MAX_WORKERS
    try:
        workers = int(str(raw).strip())
    except ValueError:
        return DEFAULT_MAX_WORKERS
    return max(0, workers)


def normalize_notify(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return list(NOTIFY_CHANNELS)
    wanted = {str(item).strip().lower() for item in raw}
    return [channel for channel in NOTIFY_CHANNELS if channel in wanted]


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
class WatchdogSettings:
    interval_seconds: int = 30
    stuck_minutes: int = 15
    spin_failures: int = 5
    context_pct: int = 80
    context_window: int | None = None
    idle_exit_minutes: int = 15


@dataclass
class Settings:
    tracking: str = "local"
    runtime: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_RUNTIME))
    models: dict[str, list[str]] = field(
        default_factory=lambda: {role: list(models) for role, models in _DEFAULT_MODELS.items()}
    )
    rubric: RubricSettings = field(default_factory=RubricSettings)
    escalation: EscalationSettings = field(default_factory=EscalationSettings)
    watchdog: WatchdogSettings = field(default_factory=WatchdogSettings)
    test_command: str | None = None
    build_command: str | None = None
    lint_command: str | None = None
    parallelism_cap: int | None = None
    time_budget_minutes: int | None = None
    # Empty means repo_check tries main, then master, then reports no base branch.
    base_branch: str | None = None
    # Per-role --effort for agent_spawn; a role with no entry runs at its model's default.
    effort: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EFFORT))
    # Per-role promptCacheTtl ("5m" or "1h") for agent_spawn; a role with no entry keeps the
    # Claude Code default.
    prompt_cache_ttl: dict[str, str] = field(default_factory=dict)
    # Per-role cap on that role's own live sessions in the run, on top of parallelism_cap.
    role_parallelism_cap: dict[str, int] = field(default_factory=dict)
    notify: list[str] = field(default_factory=lambda: list(NOTIFY_CHANNELS))
    max_workers: int = DEFAULT_MAX_WORKERS
    # Empty means setup picks mod on a Claude Code build that runs the mod, command otherwise.

    def snapshot(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_snapshot(cls, text: str) -> Settings:
        data = json.loads(text)
        return cls(
            **{
                **data,
                "rubric": RubricSettings(**data["rubric"]),
                "escalation": EscalationSettings(**data["escalation"]),
                "watchdog": WatchdogSettings(**data["watchdog"]),
            }
        )

    def adopt_profile(self, snapshot: str | None) -> None:
        try:
            data = json.loads(snapshot or "{}")
        except ValueError:
            return
        if isinstance(data, dict):
            for key in PROFILE_KEYS:
                if key in data:
                    setattr(self, key, data[key])


def _frontmatter(text: str) -> dict[str, Any]:
    parts = frontmatter.split(text)
    if parts is None:
        return {}
    loaded = frontmatter.parse(parts[0])
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

    watchdog_data = data.get("watchdog") or {}
    watchdog = WatchdogSettings(
        interval_seconds=watchdog_data.get("interval_seconds", defaults.watchdog.interval_seconds),
        stuck_minutes=watchdog_data.get("stuck_minutes", defaults.watchdog.stuck_minutes),
        spin_failures=watchdog_data.get("spin_failures", defaults.watchdog.spin_failures),
        context_pct=watchdog_data.get("context_pct", defaults.watchdog.context_pct),
        context_window=watchdog_data.get("context_window", defaults.watchdog.context_window),
        idle_exit_minutes=watchdog_data.get(
            "idle_exit_minutes", defaults.watchdog.idle_exit_minutes
        ),
    )

    effort = (
        {role: str(level) for role, level in (data.get("effort") or {}).items() if level}
        if "effort" in data
        else dict(DEFAULT_EFFORT)
    )
    prompt_cache_ttl = {
        role: str(ttl) for role, ttl in (data.get("prompt_cache_ttl") or {}).items() if ttl
    }
    role_parallelism_cap = {
        role: int(cap) for role, cap in (data.get("role_parallelism_cap") or {}).items() if cap
    }

    return Settings(
        tracking=data.get("tracking") or defaults.tracking,
        runtime=runtime,
        models=models,
        rubric=rubric,
        escalation=escalation,
        watchdog=watchdog,
        test_command=data.get("test_command") or defaults.test_command,
        build_command=data.get("build_command") or defaults.build_command,
        lint_command=data.get("lint_command") or defaults.lint_command,
        parallelism_cap=data.get("parallelism_cap") or defaults.parallelism_cap,
        time_budget_minutes=normalize_budget(data.get("time_budget_minutes")),
        base_branch=data.get("base_branch") or defaults.base_branch,
        effort=effort,
        prompt_cache_ttl=prompt_cache_ttl,
        role_parallelism_cap=role_parallelism_cap,
        notify=normalize_notify(data.get("notify")),
        max_workers=normalize_max_workers(data.get("max_workers")),
    )
