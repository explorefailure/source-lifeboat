#!/usr/bin/env python3
"""Destructive disposable-only integration check for the trusted runner.

The runner must supply the exact Forgejo URL and an installation identity. The
identity is proven by a private repository named ``rb-installation-sentinel``
whose description is exactly ``repository-backup-disposable:<identity>``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

from repository_backup.downloader import SafeDownloader
from repository_backup.forgejo import ForgejoClient, ForgejoError, archive_repository_name
from repository_backup.github import GitHubClient, parse_repository_url
from repository_backup.manifest import build_manifest, read_manifest, write_manifest


class Refusal(RuntimeError):
    pass


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise Refusal(f"required environment variable {name} is missing")
    return value


def validate_environment() -> dict[str, str]:
    if required("RB_TEST_ALLOW_DISPOSABLE") != "1":
        raise Refusal("RB_TEST_ALLOW_DISPOSABLE must be exactly 1")
    values = {name: required(name) for name in (
        "RB_TEST_FORGEJO_URL", "RB_TEST_FORGEJO_TOKEN", "RB_TEST_FORGEJO_OWNER", "RB_TEST_INSTALLATION_ID",
    )}
    if required("RB_TEST_REAL_REPOSITORY") != "sharkdp/hexyl":
        raise Refusal("RB_TEST_REAL_REPOSITORY must be exactly sharkdp/hexyl")
    url = values["RB_TEST_FORGEJO_URL"]
    if url != url.rstrip("/") or not url.startswith(("http://", "https://")):
        raise Refusal("RB_TEST_FORGEJO_URL must be the operator-provided exact base URL without a trailing slash")
    if len(values["RB_TEST_INSTALLATION_ID"]) < 12:
        raise Refusal("RB_TEST_INSTALLATION_ID is not a sufficiently unique identity")
    keep_result = os.environ.get("RB_TEST_KEEP_RESULT", "0")
    if keep_result not in ("0", "1"):
        raise Refusal("RB_TEST_KEEP_RESULT must be exactly 0 or 1 when set")
    values["RB_TEST_KEEP_RESULT"] = keep_result
    return values


def verify_installation(client: ForgejoClient, installation_id: str) -> None:
    owner = urllib.parse.quote(client.owner, safe="")
    sentinel = client._request("GET", f"/api/v1/repos/{owner}/rb-installation-sentinel")
    expected = f"repository-backup-disposable:{installation_id}"
    if sentinel.get("private") is not True or sentinel.get("description") != expected:
        raise Refusal("disposable Forgejo sentinel identity does not match the exact caller-provided installation identity")


class _FixtureResponse(io.BytesIO):
    def __init__(self, body: bytes, *, json_value=None):
        if json_value is not None:
            body = json.dumps(json_value).encode()
        super().__init__(body); self.status = 200; self.headers = {"Content-Length": str(len(body))}
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class _FixtureGitHub:
    def __init__(self):
        self.latest = {
            "id": 1, "tag_name": "v1", "draft": False, "prerelease": False,
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/fixture/project/releases/tag/v1",
            "assets": [{"id": 2, "name": "fixture.bin", "size": 5, "url": "https://api.github.com/repos/fixture/project/releases/assets/2"}],
        }
    def open(self, request, timeout=None):
        path = request.full_url.removeprefix("https://api.github.com")
        if path.startswith("/repos/fixture/project/releases/1/assets?"): return _FixtureResponse(b"", json_value=self.latest["assets"])
        if path == "/repos/fixture/project/releases/latest": return _FixtureResponse(b"", json_value=self.latest)
        if path == "/repos/fixture/project/git/ref/tags/v1": return _FixtureResponse(b"", json_value={"object":{"type":"commit","sha":"a"*40}})
        raise AssertionError(path)


class _FixtureFiles:
    def open(self, request, timeout=None):
        body = b"asset" if request.full_url.endswith("/2") else b"source"
        return _FixtureResponse(body)


def fixture_check(root: Path) -> int:
    repository = parse_repository_url("https://github.com/fixture/project")
    client = GitHubClient(opener=_FixtureGitHub())
    release = client.latest_release(repository)
    downloader = SafeDownloader(
        root, opener=_FixtureFiles(), resolver=lambda host: ["8.8.8.8"], min_free_bytes=0,
        max_file_bytes=1024, max_total_bytes=4096,
    )
    files = downloader.download_release(release)
    client.assert_unchanged(repository, release)
    manifest = build_manifest(repository, release, files, retrieved_at="2026-01-01T00:00:00+00:00")
    path = write_manifest(root / "manifest.json", manifest)
    if read_manifest(path)["files"][1]["sha256"] != hashlib.sha256(b"asset").hexdigest():
        raise RuntimeError("fixture end-to-end checksum failed")
    return len(files)


def ensure_target_absent(client: ForgejoClient, repository: str) -> None:
    owner = urllib.parse.quote(client.owner, safe="")
    try:
        client._request("GET", f"/api/v1/repos/{owner}/{repository}")
    except ForgejoError as exc:
        if "HTTP 404" in str(exc):
            return
        raise
    raise Refusal(f"integration target repository {repository} already exists; refusing to discover, reuse or alter it")


def restore_and_verify(manifest_path: Path, source_dir: Path, restore_dir: Path) -> int:
    manifest = read_manifest(manifest_path)
    count = 0
    for item in manifest["files"]:
        source = source_dir / item["stored_name"]
        target = restore_dir / item["stored_name"]
        shutil.copyfile(source, target)
        digest = hashlib.sha256()
        size = 0
        with target.open("rb") as stream:
            while block := stream.read(64 * 1024):
                size += len(block); digest.update(block)
        if size != item["size"] or digest.hexdigest() != item["sha256"]:
            raise RuntimeError(f"restore verification failed for {item['stored_name']}")
        count += 1
    return count


def main() -> int:
    values = validate_environment()
    max_file = int(os.environ.get("RB_TEST_MAX_FILE_BYTES", str(512 * 1024**2)))
    max_total = int(os.environ.get("RB_TEST_MAX_TOTAL_BYTES", str(2 * 1024**3)))
    client = ForgejoClient(
        values["RB_TEST_FORGEJO_URL"], values["RB_TEST_FORGEJO_TOKEN"], values["RB_TEST_FORGEJO_OWNER"],
        public_url=os.environ.get("RB_TEST_FORGEJO_PUBLIC_URL", values["RB_TEST_FORGEJO_URL"]),
        max_attachment_bytes=max_file,
    )
    verify_installation(client, values["RB_TEST_INSTALLATION_ID"])
    target_repository = archive_repository_name("sharkdp", "hexyl")
    ensure_target_absent(client, target_repository)
    created = False
    with tempfile.TemporaryDirectory(prefix="repository-backup-integration-") as temp_name:
        root = Path(temp_name)
        fixture_files = fixture_check(root / "fixture")
        repository = parse_repository_url("https://github.com/sharkdp/hexyl")
        github = GitHubClient()
        release = github.latest_release(repository)
        stage = root / "real"; stage.mkdir()
        files = SafeDownloader(
            stage, max_file_bytes=max_file, max_total_bytes=max_total,
            min_free_bytes=64 * 1024**2,
        ).download_release(release)
        if not release.assets:
            raise RuntimeError("real release unexpectedly has no uploaded assets")
        github.assert_unchanged(repository, release)
        manifest_path = write_manifest(stage / "manifest.json", build_manifest(repository, release, files))
        report = None
        try:
            # The exact target was proven absent, so any repository at this name
            # after this point belongs to this run and is safe to clean up.
            created = True
            publication = client.publish(repository.owner, repository.name, release, files, manifest_path)
            restored = root / "restore"; restored.mkdir()
            restored_files = restore_and_verify(manifest_path, stage, restored)
            report = {
                "ok": True,
                "fixture_files_checked": fixture_files,
                "real_repository": "sharkdp/hexyl",
                "github_release_id": release.id,
                "release_tag": release.tag,
                "commit_sha": release.commit_sha,
                "assets_checked": len(release.assets),
                "files_restored": restored_files,
                "forgejo_release_id": publication.release_id,
                "result_repository_retained": True,
                "attachment_verification": "streamed-size-and-sha256",
                "limitations": [
                    "The trusted operator, not this script, verifies Compose project labels and second-instance container/volume isolation.",
                    "Checksums establish transfer integrity, not publisher authenticity.",
                    "Git LFS objects and submodule content may be absent from GitHub source archives.",
                ],
            }
        finally:
            # This unprivileged script cannot inspect trusted Docker labels.
            # Retain the disposable repository; only the operator may clean up
            # after validating both the installation sentinel and project labels.
            pass
        if report is not None:
            print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        # Tokens and request headers are never included in application exceptions.
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
