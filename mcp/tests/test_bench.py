from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load("bench_under_test", SCRIPTS / "bench" / "bench.py")
host_settings = _load("host_settings_under_test", SCRIPTS / "host_settings.py")


def _checklist(tmp_path: Path, state: str, started_at: str = "2026-01-01T00:00:00.000Z") -> Path:
    path = tmp_path / "checklist.json"
    doc = {
        "passed": True,
        "checks": [{"name": "x", "status": "fail", "detail": ""}],
        "metrics": {
            "run": {"state": state, "started_at": started_at, "wall_seconds": 30.0},
            "cost_usd": {"total": 1.5},
        },
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _grade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str, marker: str = "marker.txt"
) -> dict:
    acceptance = tmp_path / "acc"
    acceptance.mkdir(exist_ok=True)
    (acceptance / "test_ok.py").write_text(
        f"def test_host(host):\n    assert (host / {marker!r}).is_file()\n", encoding="utf-8"
    )
    host = tmp_path / "host"
    host.mkdir(exist_ok=True)
    (host / "marker.txt").write_text("x", encoding="utf-8")
    argv = [
        "grade",
        "--host",
        str(host),
        "--acceptance",
        str(acceptance),
        "--work",
        str(tmp_path / "trial"),
        "--case",
        "hello",
        "--trial",
        "1",
        *extra,
    ]
    assert bench.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_override_pins_models_and_effort(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "override.json"
    argv = ["override", "--prd", "hello", "--model-pin", "all=m1", "--model-pin", "oracle=m2"]
    assert bench.main([*argv, "--effort", "coder=low", "--out", str(out)]) == 0
    variant = json.loads(capsys.readouterr().out)
    settings = json.loads(out.read_text(encoding="utf-8"))
    assert settings["models"] == {
        "oracle": ["m2"],
        "manager": ["m1"],
        "lead": ["m1"],
        "coder": ["m1"],
        "driver": ["m1"],
    }
    assert settings["effort"] == {"coder": "low"}
    assert variant["model_pins"]["oracle"] == "m2"


@pytest.mark.parametrize(
    "argv", [["--model-pin", "boss=m"], ["--effort", "coder=huge"], ["--model-pin", "coder"]]
)
def test_override_rejects_bad_pairs(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        bench.main(["override", "--prd", "hello", *argv])


def test_grade_passes_a_finished_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checklist = _checklist(tmp_path, "finished")
    line = _grade(tmp_path, capsys, "--checklist", str(checklist))
    assert line["passed"] is True
    assert line["acceptance"]["tests"] == 1
    assert line["checklist"] == {"passed": True, "failed": ["x"]}
    assert not (tmp_path / "host" / "test_ok.py").exists()


def test_grade_fails_an_unfinished_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line = _grade(tmp_path, capsys, "--checklist", str(_checklist(tmp_path, "active")))
    assert line["passed"] is False
    assert line["acceptance"] is None
    assert "did not finish" in line["reason"]


def test_grade_fails_a_run_older_than_the_trial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checklist = _checklist(tmp_path, "finished")
    line = _grade(
        tmp_path, capsys, "--checklist", str(checklist), "--since", "2026-06-01T00:00:00Z"
    )
    assert line["passed"] is False
    assert line["reason"] == "no run started in this trial"


def test_grade_fails_a_failing_acceptance_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checklist = _checklist(tmp_path, "finished")
    line = _grade(tmp_path, capsys, "--checklist", str(checklist), marker="nope.txt")
    assert line["passed"] is False
    assert line["reason"] == "an acceptance test failed"
    assert line["acceptance"]["failures"] == 1


def test_summary_reports_pass_k(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trials = tmp_path / "trials.jsonl"
    rows = [
        {"case": "hello", "passed": True, "metrics": {"cost_usd": {"total": 1.0}, "run": {}}},
        {
            "case": "hello",
            "passed": False,
            "metrics": {"cost_usd": {"total": 3.0}, "run": {"wall_seconds": 60}},
        },
        {"case": "hello", "passed": True, "metrics": None},
    ]
    trials.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    out = tmp_path / "summary.json"
    assert bench.main(["summary", "--trials", str(trials), "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert (doc["trials"], doc["passes"], doc["pass^k"]) == (3, 2, False)
    assert doc["cost_usd"] == {"mean": 2.0, "median": 2.0, "n": 2}
    assert doc["wall_seconds"]["n"] == 1
    assert "2/3 passed" in capsys.readouterr().out


def test_host_settings_merges_and_pins_the_oracle(tmp_path: Path) -> None:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (tmp_path / ".claude" / "sentinel-swarm.local.md").write_text(
        "---\nmodels:\n  oracle: [opus]\n  coder: [sonnet, haiku]\neffort: {}\ntracking: local\n"
        "---\n\n# body\n",
        encoding="utf-8",
    )
    (agents / "swarm-oracle.md").write_text(
        "---\nname: swarm-oracle\nmodel: opus\n---\nprompt\n", encoding="utf-8"
    )
    override = {"models": {"oracle": ["o-1"], "lead": ["l-1"]}, "effort": {"lead": "high"}}
    host_settings.apply(tmp_path, override)
    text = (tmp_path / ".claude" / "sentinel-swarm.local.md").read_text(encoding="utf-8")
    head, body = host_settings.split(text)
    data = yaml.safe_load(head)
    assert data["models"] == {"oracle": ["o-1"], "coder": ["sonnet", "haiku"], "lead": ["l-1"]}
    assert data["effort"] == {"lead": "high"}
    assert data["tracking"] == "local"
    assert body == "\n# body\n"
    oracle = (agents / "swarm-oracle.md").read_text(encoding="utf-8")
    assert oracle == "---\nname: swarm-oracle\nmodel: o-1\n---\nprompt\n"
