from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROLES = ("oracle", "manager", "lead", "coder", "driver")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
HERE = Path(__file__).resolve().parent
CONFTEST = HERE / "acceptance_conftest.py"


def _pairs(values: list[str], flag: str, allowed: tuple[str, ...] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in values:
        role, sep, value = raw.partition("=")
        role, value = role.strip().lower(), value.strip()
        if not sep or not value:
            raise SystemExit(f"{flag} takes role=value, got {raw!r}")
        if role != "all" and role not in ROLES:
            raise SystemExit(f"{flag}: unknown role {role!r}; use one of {', '.join(ROLES)}, all")
        if allowed is not None and value not in allowed:
            raise SystemExit(f"{flag}: unknown level {value!r}; use one of {', '.join(allowed)}")
        for name in ROLES if role == "all" else (role,):
            out[name] = value
    return out


def _output(command: list[str], cwd: Path | None = None) -> str | None:
    try:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def override(args: argparse.Namespace) -> int:
    pins = _pairs(args.model_pin, "--model-pin", None)
    efforts = _pairs(args.effort, "--effort", EFFORTS)
    settings: dict[str, Any] = {}
    if args.settings:
        loaded = json.loads(Path(args.settings).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise SystemExit(f"{args.settings} does not hold a JSON object")
        settings.update(loaded)
    if pins:
        settings["models"] = {**settings.get("models", {}), **{r: [m] for r, m in pins.items()}}
    if efforts:
        settings["effort"] = {**settings.get("effort", {}), **efforts}
    root = HERE.parents[1]
    claude = os.environ.get("CLAUDE_BIN", "claude")
    variant = {
        "label": args.label or None,
        "prd": args.prd,
        "model_pins": pins,
        "effort": efforts,
        "settings": settings,
        "claude_version": _output([claude, "--version"]),
        "commit": _output(["git", "rev-parse", "HEAD"], root),
        "dirty": bool(_output(["git", "status", "--porcelain"], root)),
    }
    if args.out:
        _write_json(Path(args.out), settings)
    print(json.dumps(variant, indent=2))
    return 0


def _write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(data, indent=2) + "\n")


def _junit(path: Path) -> dict[str, int]:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return counts
    suites = tree.getroot().iter("testsuite")
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key) or 0)
    return counts


def run_acceptance(acceptance: Path, host: Path, work: Path) -> dict[str, Any]:
    target = work / "acceptance"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(acceptance, target)
    if not (target / "conftest.py").exists():
        shutil.copyfile(CONFTEST, target / "conftest.py")
    (target / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    report = work / "acceptance.xml"
    env = dict(os.environ, BENCH_HOST=str(host), PYTHONDONTWRITEBYTECODE="1")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
        str(target),
    ]
    try:
        done = subprocess.run(
            command, cwd=target, env=env, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "error": "the acceptance tests timed out", **_junit(report)}
    (work / "acceptance.txt").write_text(done.stdout + done.stderr, encoding="utf-8")
    return {"exit_code": done.returncode, **_junit(report)}


def _stamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stale(run: dict[str, Any], since: str | None) -> bool:
    started, floor = _stamp(run.get("started_at")), _stamp(since)
    return floor is not None and (started is None or started < floor)


def grade(args: argparse.Namespace) -> int:
    host, work = Path(args.host).resolve(), Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    try:
        checklist = json.loads(Path(args.checklist).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        checklist = None
    metrics = (checklist or {}).get("metrics")
    run = (metrics or {}).get("run") or {}
    acceptance = None
    if _stale(run, args.since):
        passed, reason = False, "no run started in this trial"
        metrics = None
    elif run.get("state") != "finished":
        passed, reason = False, f"the run did not finish (state {run.get('state')!r})"
    else:
        acceptance = run_acceptance(Path(args.acceptance), host, work)
        passed = acceptance["exit_code"] == 0 and acceptance["tests"] > 0
        reason = "" if passed else acceptance.get("error") or "an acceptance test failed"
    line = {
        "schema": 1,
        "case": args.case,
        "label": args.label or None,
        "trial": args.trial,
        "passed": passed,
        "reason": reason,
        "acceptance": acceptance,
        "checklist": None
        if checklist is None
        else {
            "passed": checklist.get("passed"),
            "failed": [c["name"] for c in checklist.get("checks", []) if c["status"] == "fail"],
        },
        "metrics": metrics,
    }
    print(json.dumps(line, sort_keys=False))
    return 0


def _spread(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"mean": None, "median": None, "n": 0}
    return {
        "mean": round(statistics.mean(values), 4),
        "median": round(statistics.median(values), 4),
        "n": len(values),
    }


def summary(args: argparse.Namespace) -> int:
    lines = [
        json.loads(line)
        for line in Path(args.trials).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    passes = sum(1 for line in lines if line["passed"])
    costs, walls = [], []
    for line in lines:
        m = line.get("metrics") or {}
        if (m.get("cost_usd") or {}).get("total") is not None:
            costs.append(float(m["cost_usd"]["total"]))
        if (m.get("run") or {}).get("wall_seconds") is not None:
            walls.append(float(m["run"]["wall_seconds"]))
    variant = json.loads(Path(args.variant).read_text(encoding="utf-8")) if args.variant else None
    doc = {
        "schema": 1,
        "case": lines[0]["case"] if lines else None,
        "trials": len(lines),
        "passes": passes,
        "pass_rate": round(passes / len(lines), 4) if lines else None,
        "pass^k": bool(lines) and passes == len(lines),
        "cost_usd": _spread(costs),
        "wall_seconds": _spread(walls),
        "variant": variant,
    }
    _write_json(Path(args.out), doc)
    print(
        f"{doc['case']}: {passes}/{len(lines)} passed, pass^k={doc['pass^k']}, "
        f"cost mean={doc['cost_usd']['mean']} median={doc['cost_usd']['median']}, "
        f"wall mean={doc['wall_seconds']['mean']}s median={doc['wall_seconds']['median']}s"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("override", help="write the settings override; print the variant")
    p.add_argument("--prd", required=True)
    p.add_argument("--model-pin", action="append", default=[])
    p.add_argument("--effort", action="append", default=[])
    p.add_argument("--settings", help="a JSON object merged into the host settings")
    p.add_argument("--label", default="")
    p.add_argument("--out")
    p.set_defaults(func=override)

    p = sub.add_parser("grade", help="print one trial's JSON line")
    p.add_argument("--host", required=True)
    p.add_argument("--acceptance", required=True)
    p.add_argument("--checklist", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--trial", type=int, required=True)
    p.add_argument("--label", default="")
    p.add_argument("--since", help="UTC time the trial started; an older run does not count")
    p.set_defaults(func=grade)

    p = sub.add_parser("summary", help="write summary.json from trials.jsonl")
    p.add_argument("--trials", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--variant")
    p.set_defaults(func=summary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
