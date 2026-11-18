"""Strict GitHub release resolution using only public REST API endpoints."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$")


class GitHubError(RuntimeError):
    pass


class NoRelease(GitHubError):
    pass


class Response(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> "Response": ...

    def __exit__(self, *args: object) -> None: ...


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    size: int
    url: str


@dataclass(frozen=True)
class Release:
    id: int
    tag: str
    commit_sha: str
    source_url: str
    html_url: str
    published_at: str
    assets: tuple[Asset, ...]


def parse_repository_url(value: str) -> Repository:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("repository URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ValueError("repository URL is invalid") from exc
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("only https://github.com/OWNER/REPO is accepted")
    if parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment:
        raise ValueError("repository URL must not contain credentials, ports, queries or fragments")
    if parsed.path.endswith("/") or parsed.path.count("/") != 2:
        raise ValueError("repository URL must contain exactly an owner and repository")
    owner, name = parsed.path[1:].split("/", 1)
    if name.endswith(".git") or not NAME.fullmatch(owner) or not NAME.fullmatch(name):
        raise ValueError("invalid GitHub owner or repository name")
    return Repository(owner, name)


class GitHubClient:
    def __init__(self, *, token: str | None = None, opener: Any = None, timeout: float = 20.0):
        self.token = token or None
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout

    def _json(self, path: str, *, expect_list: bool = False) -> Any:
        if not path.startswith("/") or "//" in path:
            raise GitHubError("invalid API path")
        request = urllib.request.Request(
            "https://api.github.com" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "repository-backup/1",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            with response:
                status = getattr(response, "status", 200)
                body = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NoRelease("repository has no published release") from exc
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                reset = exc.headers.get("X-RateLimit-Reset", "unknown")
                raise GitHubError(f"GitHub API quota exhausted; retry after reset (Unix time {reset})") from exc
            raise GitHubError(f"GitHub API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError(f"GitHub API request failed: {exc}") from exc
        if status == 404:
            raise NoRelease("repository has no published release")
        if status != 200:
            raise GitHubError(f"GitHub API returned HTTP {status}")
        if len(body) > 2 * 1024 * 1024:
            raise GitHubError("GitHub API response is too large")
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubError("GitHub API returned invalid JSON") from exc
        if not isinstance(value, list if expect_list else dict):
            raise GitHubError("GitHub API returned an unexpected value")
        return value

    @staticmethod
    def _required(value: dict[str, Any], key: str, kind: type) -> Any:
        result = value.get(key)
        if not isinstance(result, kind):
            raise GitHubError(f"GitHub response has invalid {key}")
        return result

    def _peel_tag(self, repository: Repository, tag: str) -> str:
        encoded = urllib.parse.quote(tag, safe="")
        reference = self._json(f"/repos/{repository.owner}/{repository.name}/git/ref/tags/{encoded}")
        obj = self._required(reference, "object", dict)
        for _ in range(8):
            object_type = self._required(obj, "type", str)
            sha = self._required(obj, "sha", str)
            if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                raise GitHubError("tag resolved to an invalid object ID")
            if object_type == "commit":
                return sha.lower()
            if object_type != "tag":
                raise GitHubError(f"tag points to unsupported object type {object_type}")
            tag_object = self._json(f"/repos/{repository.owner}/{repository.name}/git/tags/{sha}")
            obj = self._required(tag_object, "object", dict)
        raise GitHubError("annotated tag nesting is too deep")

    def latest_release(self, repository: Repository | str) -> Release:
        if isinstance(repository, str):
            repository = parse_repository_url(repository)
        value = self._json(f"/repos/{repository.owner}/{repository.name}/releases/latest")
        if value.get("draft") is not False or value.get("prerelease") is not False:
            raise NoRelease("repository has no published stable release")
        release_id = self._required(value, "id", int)
        tag = self._required(value, "tag_name", str)
        published = self._required(value, "published_at", str)
        html_url = self._required(value, "html_url", str)
        if not tag or not published or not html_url.startswith(repository.url + "/releases/"):
            raise GitHubError("release metadata is incomplete")
        commit = self._peel_tag(repository, tag)
        assets_value = []
        for page in range(1, 102):
            batch = self._json(f"/repos/{repository.owner}/{repository.name}/releases/{release_id}/assets?per_page=100&page={page}", expect_list=True)
            assets_value.extend(batch)
            if len(assets_value) > 10000:
                raise GitHubError("release asset count exceeds the supported bound")
            if len(batch) < 100:
                break
        else:
            raise GitHubError("release asset pagination exceeds the supported bound")
        assets: list[Asset] = []
        seen_ids: set[int] = set()
        for item in assets_value:
            if not isinstance(item, dict):
                raise GitHubError("release asset metadata is invalid")
            asset_id = self._required(item, "id", int)
            name = self._required(item, "name", str)
            size = self._required(item, "size", int)
            url = self._required(item, "url", str)
            if asset_id in seen_ids or not name or size < 0:
                raise GitHubError("release asset metadata is invalid")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "api.github.com":
                raise GitHubError("release asset URL is not an official GitHub API URL")
            seen_ids.add(asset_id)
            assets.append(Asset(asset_id, name, size, url))
        return Release(
            release_id,
            tag,
            commit,
            f"https://api.github.com/repos/{repository.owner}/{repository.name}/tarball/{commit}",
            html_url,
            published,
            tuple(assets),
        )

    def assert_unchanged(self, repository: Repository | str, expected: Release) -> Release:
        current = self.latest_release(repository)
        before = (expected.id, expected.tag, expected.commit_sha, tuple((a.id, a.name, a.size) for a in expected.assets))
        after = (current.id, current.tag, current.commit_sha, tuple((a.id, a.name, a.size) for a in current.assets))
        if before != after:
            raise GitHubError("upstream release changed while it was being downloaded")
        return current
