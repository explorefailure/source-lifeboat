"""Canonical versioned backup manifest."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


WARNINGS = [
    "Git LFS objects may be absent from the source archive.",
    "Git submodule content is not included in the source archive.",
    "A SHA-256 checksum proves local file integrity, not publisher authenticity.",
]


def build_manifest(repository: Any, release: Any, files: Iterable[Any], *, retrieved_at: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": {"owner": repository.owner, "name": repository.name, "url": repository.url},
        "release": {
            "github_release_id": release.id,
            "tag": release.tag,
            "commit_sha": release.commit_sha,
            "source_url": release.source_url,
            "html_url": release.html_url,
            "published_at": release.published_at,
            "retrieved_at": retrieved_at or datetime.now(UTC).isoformat(),
        },
        "files": [
            {
                "kind": item.kind,
                "original_filename": item.original_filename,
                "stored_name": item.stored_name,
                "size": item.size,
                "sha256": item.sha256,
                "upstream_asset_id": item.upstream_asset_id,
            }
            for item in files
        ],
        "warnings": WARNINGS,
    }


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    target = Path(path)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    temporary = target.with_suffix(target.suffix + ".partial")
    temporary.write_bytes(encoded)
    temporary.replace(target)
    return target


def read_manifest(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("files"), list):
        raise ValueError("unsupported or invalid manifest")
    return value


def same_release_contents(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Ignore only retrieval time and file ordering when reusing a snapshot."""
    def identity(manifest):
        release = dict(manifest.get("release", {}))
        release.pop("retrieved_at", None)
        files = sorted(manifest.get("files", []), key=lambda item: json.dumps(item, sort_keys=True))
        return {"schema": manifest.get("schema_version"), "repository": manifest.get("repository"), "release": release, "files": files}
    return identity(previous) == identity(current)
