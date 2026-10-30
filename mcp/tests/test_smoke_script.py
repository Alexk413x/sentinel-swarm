from __future__ import annotations

from pathlib import Path


def test_smoke_sh_rewrites_a_dev_version_before_install(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    install_at = text.index('plugin marketplace add --scope project "$(win "$plugin_dir")"')
    version_at = text.index("d['version']=sys.argv[2]")
    assert version_at < install_at


def test_smoke_sh_installs_from_a_marketplace_named_for_the_run(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    assert 'market="sentinel-swarm-smoke-$(date +%s)-$$"' in text
    assert 'plugin_dir="${TMPDIR:-/tmp}/$market"' in text
    assert '"name": "$market",' in text
    assert 'plugin install "sentinel-swarm@$market"' in text
    assert 'installed="$config_dir/plugins/cache/$market/sentinel-swarm/$version"' in text
    assert "sentinel-swarm@sentinel-swarm" not in text


def test_smoke_sh_tears_down_only_its_own_earlier_marketplace(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    teardown = text[text.index("teardown() {") : text.index("teardown_after_run() {")]
    assert 'old="$(cat "$market_file"' in teardown
    assert 'plugin marketplace remove "$old"' in teardown
    assert 'rm -rf "${TMPDIR:-/tmp}/$old" "$config_dir/plugins/cache/$old"' in teardown
    assert "sentinel-swarm-plugin" not in text
    assert text.index('echo "$market" > "$market_file"') < text.index("plugin marketplace add")


def test_smoke_sh_results_runs_the_checklist(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    results_at = text.index('mode" = results')
    checklist_at = text.index('ledger_at "$root" checklist')
    exit_at = text.index('exit "$rc"')
    assert results_at < checklist_at < exit_at
