from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from swarm_ledger import frontmatter

REPO = Path(__file__).resolve().parents[2]
SHIPPED = [
    REPO / "templates" / "sentinel-swarm.local.md.example",
    *sorted((REPO / "templates" / "agents").glob("*.md")),
]

SAMPLES = [
    "a: 1\nb: -2\nc: 3.5\nd: true\ne: no\nf: ~\ng:\nh: text, with commas\n",
    "list: [a, 'b c', \"d\\te\", 4]\nmap: {x: 1, y: [2, 3]}\n",
    "multi: [one,\n  two,\n  three]\n",
    "seq:\n  - a\n  - b # comment\n  -\n    nested: 1\n",
    "same:\n- a\n- b\nnext: 1\n",
    "items:\n  - name: one\n    args: [x]\n  - name: two\n    deep:\n      k: v\n",
    "quoted: 'it''s'\nkey with space: v\n\"quoted key\": 2\n",
    "url: http://127.0.0.1:8000/mcp\npath: C:\\Users\\x\n",
    "hash: a#b\ncomment: value # gone\nempty_list: []\nempty_map: {}\n",
    "literal: |\n  line one\n  line two\n\nfolded: >\n  folded\n  text\n\n  para\nafter: 1\n",
    "strip: |-\n  kept\nnum: 0\nfloat: .5\nexp: 1e3\n",
    "# only a comment\nkey: value\n",
]


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_shipped_frontmatter_parses_as_pyyaml_does(path: Path) -> None:
    parts = frontmatter.split(path.read_text(encoding="utf-8"))
    assert parts is not None
    assert frontmatter.parse(parts[0]) == yaml.safe_load(parts[0])


@pytest.mark.parametrize("text", SAMPLES)
def test_the_yaml_subset_parses_as_pyyaml_does(text: str) -> None:
    assert frontmatter.parse(text) == yaml.safe_load(text)


def test_split_returns_the_frontmatter_and_the_body() -> None:
    assert frontmatter.split("---\na: 1\n---\nbody\n") == ("a: 1", "body")
    assert frontmatter.split("no frontmatter") is None
    assert frontmatter.split("---\na: 1\n") is None


@pytest.mark.parametrize("text", ["a: [1, 2", "a: 1\n b: 2", "- a\nb: 1", 'a: "\\q"'])
def test_malformed_frontmatter_raises(text: str) -> None:
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse(text)
