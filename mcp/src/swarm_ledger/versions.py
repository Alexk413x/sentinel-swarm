from __future__ import annotations

import hashlib
from pathlib import Path


def save_version(records_dir: Path, file_id: int, agent_id: str | None, path: Path) -> dict:
    content = path.read_bytes()
    version_dir = records_dir / "versions" / str(file_id)
    version_dir.mkdir(parents=True, exist_ok=True)
    existing = [int(p.stem) for p in version_dir.glob("*.bin") if p.stem.isdigit()]
    n = max(existing, default=0) + 1
    stored_file = version_dir / f"{n}.bin"
    stored_file.write_bytes(content)

    return {
        "file_id": file_id,
        "agent_id": agent_id,
        "content": content,
        "sha256": hashlib.sha256(content).hexdigest(),
        "stored_path": stored_file.relative_to(records_dir).as_posix(),
    }


def restore_version(records_dir: Path, stored_path: str, target: Path) -> None:
    source = Path(stored_path)
    if not source.is_absolute():
        source = records_dir / source
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
