from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

from . import frontmatter
from .permissions import PERMISSION_MODES
from .rubric import Thresholds

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
    "driver": ["sonnet", "opus"],
}
DEFAULT_EFFORT = dict.fromkeys(_DEFAULT_MODELS, "medium")
SETTINGS_PATH = Path(".claude") / "sentinel-swarm.local.md"
NOTIFY_CHANNELS = ("os", "push")
DEFAULT_MAX_WORKERS = 8
DEFAULT_HEADROOM_MB = 1500
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})
PROFILE_KEYS = ("test_command", "build_command", "lint_command")
TEST_SCOPES = ("file", "module", "phase", "full")
DEFAULT_TEST_TIMEOUT_S = 600


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


def normalize_test_timeout(raw: object) -> int:
    if raw is None or isinstance(raw, bool):
        return DEFAULT_TEST_TIMEOUT_S
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return DEFAULT_TEST_TIMEOUT_S
    return seconds if seconds > 0 else DEFAULT_TEST_TIMEOUT_S


def normalize_text_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if item is not None and str(item).strip()]


def normalize_scope_commands(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        scope: str(raw[scope]).strip()
        for scope in TEST_SCOPES
        if raw.get(scope) is not None and str(raw[scope]).strip()
    }


def normalize_notify(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return list(NOTIFY_CHANNELS)
    wanted = {str(item).strip().lower() for item in raw}
    return [channel for channel in NOTIFY_CHANNELS if channel in wanted]


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
    unsent_wakeup_minutes: int = 5
    queued_spawn_minutes: int = 10
    remind_minutes: int = 30


@dataclass
class MemorySettings:
    headroom_mb: int | None = DEFAULT_HEADROOM_MB
    park_idle_parents: bool = True


def normalize_memory(raw: object) -> MemorySettings:
    data = raw if isinstance(raw, dict) else {}
    headroom: int | None = DEFAULT_HEADROOM_MB
    if "headroom_mb" in data:
        try:
            headroom = max(0, int(str(data["headroom_mb"]).strip()))
        except ValueError:
            headroom = None
    park = str(data.get("park_idle_parents", True)).strip().lower()
    return MemorySettings(headroom_mb=headroom, park_idle_parents=park not in _FALSE_WORDS)


_Section = TypeVar("_Section", Thresholds, EscalationSettings, WatchdogSettings)


@dataclass
class Settings:
    runtime: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_RUNTIME))
    models: dict[str, list[str]] = field(
        default_factory=lambda: {role: list(models) for role, models in _DEFAULT_MODELS.items()}
    )
    rubric: Thresholds = field(default_factory=Thresholds)
    escalation: EscalationSettings = field(default_factory=EscalationSettings)
    watchdog: WatchdogSettings = field(default_factory=WatchdogSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    test_command: str | None = None
    build_command: str | None = None
    lint_command: str | None = None
    test_command_by_scope: dict[str, str] = field(default_factory=dict)
    test_timeout_s: int = DEFAULT_TEST_TIMEOUT_S
    full_target: str | None = None
    allowed_skips: list[str] = field(default_factory=list)
    slow_tests: list[str] = field(default_factory=list)
    parallelism_cap: int | None = None
    time_budget_minutes: int | None = None
    # Empty means repo_check tries main, then master, then reports no base branch.
    base_branch: str | None = None
    # Per-role --effort for agent_spawn; a role with no entry runs at its model's default.
    effort: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EFFORT))
    # Per-role promptCacheTtl ("5m" or "1h") for agent_spawn; a role with no entry keeps the
    # Claude Code default.
    prompt_cache_ttl: dict[str, str] = field(default_factory=dict)
    # Per-role --permission-mode for every launch; a role with no entry inherits the user's
    # permissions.defaultMode (see permissions.start_mode).
    permission_mode: dict[str, str] = field(default_factory=dict)
    # Per-role cap on that role's own live sessions in the run, on top of parallelism_cap.
    role_parallelism_cap: dict[str, int] = field(default_factory=dict)
    notify: list[str] = field(default_factory=lambda: list(NOTIFY_CHANNELS))
    max_workers: int = DEFAULT_MAX_WORKERS

    def snapshot(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_snapshot(cls, text: str) -> Settings:
        data = json.loads(text)
        return cls(
            **{
                **data,
                "rubric": Thresholds(**data["rubric"]),
                "escalation": EscalationSettings(**data["escalation"]),
                "watchdog": WatchdogSettings(**data["watchdog"]),
                "memory": MemorySettings(**(data.get("memory") or {})),
            }
        )

    def test_template(self, scope: str) -> str | None:
        return self.test_command_by_scope.get(scope) or self.test_command

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


def _section(cls: type[_Section], raw: dict[str, Any] | None) -> _Section:
    data = raw or {}
    defaults = cls()
    return cls(**{f.name: data.get(f.name, getattr(defaults, f.name)) for f in fields(cls)})


def load_settings(repo_root: Path) -> Settings:
    path = repo_root / SETTINGS_PATH
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

    rubric = _section(Thresholds, data.get("rubric"))
    escalation = _section(EscalationSettings, data.get("escalation"))
    watchdog = _section(WatchdogSettings, data.get("watchdog"))
    memory = normalize_memory(data.get("memory"))

    effort = (
        {role: str(level) for role, level in (data.get("effort") or {}).items() if level}
        if "effort" in data
        else dict(DEFAULT_EFFORT)
    )
    prompt_cache_ttl = {
        role: str(ttl) for role, ttl in (data.get("prompt_cache_ttl") or {}).items() if ttl
    }
    permission_mode = {
        role: str(mode).strip()
        for role, mode in (data.get("permission_mode") or {}).items()
        if mode and str(mode).strip() in PERMISSION_MODES
    }
    role_parallelism_cap = {
        role: int(cap) for role, cap in (data.get("role_parallelism_cap") or {}).items() if cap
    }

    return Settings(
        runtime=runtime,
        models=models,
        rubric=rubric,
        escalation=escalation,
        watchdog=watchdog,
        memory=memory,
        test_command=data.get("test_command") or defaults.test_command,
        build_command=data.get("build_command") or defaults.build_command,
        lint_command=data.get("lint_command") or defaults.lint_command,
        test_command_by_scope=normalize_scope_commands(data.get("test_command_by_scope")),
        test_timeout_s=normalize_test_timeout(data.get("test_timeout_s")),
        full_target=str(data["full_target"]).strip() or None
        if data.get("full_target") is not None
        else None,
        allowed_skips=normalize_text_list(data.get("allowed_skips")),
        slow_tests=normalize_text_list(data.get("slow_tests")),
        parallelism_cap=data.get("parallelism_cap") or defaults.parallelism_cap,
        time_budget_minutes=normalize_budget(data.get("time_budget_minutes")),
        base_branch=data.get("base_branch") or defaults.base_branch,
        effort=effort,
        prompt_cache_ttl=prompt_cache_ttl,
        permission_mode=permission_mode,
        role_parallelism_cap=role_parallelism_cap,
        notify=normalize_notify(data.get("notify")),
        max_workers=normalize_max_workers(data.get("max_workers")),
    )
