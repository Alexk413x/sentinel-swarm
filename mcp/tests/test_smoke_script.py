from __future__ import annotations

from pathlib import Path


def test_smoke_sh_rewrites_a_dev_version_before_install(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    install_at = text.index('plugin marketplace add --scope project "$(win "$plugin_dir")"')
    version_at = text.index("d['version']=sys.argv[2]")
    assert version_at < install_at


def test_smoke_sh_cleans_up_old_dev_cache_copies_in_the_claude_config_dir(
    repo_root: Path,
) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    assert '"$config_dir/plugins/cache/sentinel-swarm/sentinel-swarm"/*-dev.*' in text


def test_smoke_sh_results_runs_the_checklist(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    results_at = text.index('mode" = results')
    checklist_at = text.index('ledger_at "$root" checklist')
    exit_at = text.index('exit "$rc"')
    assert results_at < checklist_at < exit_at
