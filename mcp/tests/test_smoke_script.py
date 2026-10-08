from __future__ import annotations

from pathlib import Path


def test_smoke_sh_rewrites_a_dev_version_before_install(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    install_at = text.index("plugin marketplace add")
    version_at = text.index("d['version']=sys.argv[2]")
    assert version_at < install_at
    assert 'dev_version="$base_version-dev.' in text


def test_smoke_sh_cleans_up_old_dev_cache_copies(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    assert 'plugins/cache/sentinel-swarm/sentinel-swarm"/*-dev.*' in text


def test_smoke_sh_results_runs_the_checklist(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    results_at = text.index('mode" = results')
    checklist_at = text.index("swarm_ledger.checklist")
    exit_at = text.index('exit "$rc"')
    assert results_at < checklist_at < exit_at
