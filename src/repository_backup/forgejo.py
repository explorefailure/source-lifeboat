"""Forgejo publication with idempotent draft repair and streamed verification."""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import mimetypes
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def archive_repository_name(owner: str, repository: str) -> str:
    owner, repository = owner.lower(), repository.lower()
    return f"{len(owner)}-{owner}-{repository}"


class ForgejoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Publication:
    repository: str
    release_id: int
    release_url: str
    tag: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class ForgejoClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        owner: str,
        *,
        public_url: str | None = None,
        max_attachment_bytes: int = 2 * 1024**3,
        timeout: float = 60.0,
        opener: Any = None,
        uploader: Any = None,
    ):
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid Forgejo URL")
        if not token:
            raise ValueError("Forgejo token is required")
        if not owner:
            raise ValueError("Forgejo owner is required")
        self.base_url = base_url.rstrip("/")
        self._origin = (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        public = urllib.parse.urlsplit((public_url or self.base_url).rstrip("/"))
        if public.scheme not in ("http", "https") or not public.hostname or public.username or public.password or public.query or public.fragment:
            raise ValueError("invalid Forgejo public URL")
        self.public_url = (public_url or self.base_url).rstrip("/")
        self._public_origin = (public.scheme, public.hostname, public.port or (443 if public.scheme == "https" else 80))
        self.token = token
        self.owner = owner
        self.max_attachment_bytes = max_attachment_bytes
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.uploader = uploader

    def _same_origin(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return (
            not parsed.username and not parsed.password and not parsed.fragment
            and (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) == self._origin
        )

    def _public_origin_url(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return (
            not parsed.username and not parsed.password and not parsed.fragment
            and (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) == self._public_origin
        )

    def _internal_download_url(self, url: str) -> str:
        if self._same_origin(url):
            return url
        if not self._public_origin_url(url):
            raise ForgejoError("Forgejo returned a cross-origin attachment URL")
        public = urllib.parse.urlsplit(url)
        internal = urllib.parse.urlsplit(self.base_url)
        return urllib.parse.urlunsplit((internal.scheme, internal.netloc, public.path, public.query, ""))

    def _request(
        self,
        method: str,
        path: str,
        value: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
        raw: bool = False,
    ) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise ForgejoError("invalid Forgejo API path")
        data = None if value is None else json.dumps(value).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=data,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            with response:
                status = getattr(response, "status", 200)
                body = response.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read(64 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ForgejoError(f"Forgejo request failed: {exc}") from exc
        if status not in expected:
            detail = body.decode("utf-8", "replace")[:500]
            raise ForgejoError(f"Forgejo returned HTTP {status}: {detail}")
        if raw:
            return body
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ForgejoError("Forgejo returned invalid JSON") from exc

    def ensure_repository(self, repository: str, description: str) -> dict[str, Any]:
        escaped_owner = urllib.parse.quote(self.owner, safe="")
        escaped_repo = urllib.parse.quote(repository, safe="")
        try:
            existing = self._request("GET", f"/api/v1/repos/{escaped_owner}/{escaped_repo}")
            if existing.get("private") is not True:
                raise ForgejoError("existing archive repository is not private")
            if existing.get("description") != description:
                raise ForgejoError("existing archive repository belongs to a different upstream project")
            return existing
        except ForgejoError as exc:
            if "HTTP 404" not in str(exc):
                raise
        user = self._request("GET", "/api/v1/user")
        endpoint = "/api/v1/user/repos" if user.get("login") == self.owner else f"/api/v1/orgs/{escaped_owner}/repos"
        return self._request(
            "POST", endpoint,
            {"name": repository, "description": description, "private": True, "auto_init": True, "default_branch": "main"},
            expected=(201,),
        )

    def _catalog_commit(self, repository: str, release_id: int, manifest_path: Path, default_branch: str) -> str:
        manifest_bytes = manifest_path.read_bytes()
        content = base64.b64encode(manifest_bytes).decode("ascii")
        path = (
            f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}"
            f"/contents/catalog/{release_id}.json"
        )
        payload = {
            "branch": default_branch,
            "content": content,
            "message": f"Archive catalog metadata for GitHub release {release_id} (not upstream history)",
        }
        try:
            existing = self._request("GET", path + "?" + urllib.parse.urlencode({"ref": default_branch}))
        except ForgejoError as exc:
            if "HTTP 404" not in str(exc):
                raise
        else:
            encoded = existing.get("content")
            if not isinstance(encoded, str):
                raise ForgejoError("existing catalog manifest has no content")
            try:
                existing_bytes = base64.b64decode(encoded, validate=False)
            except (ValueError, binascii.Error) as exc:
                raise ForgejoError("existing catalog manifest is invalid") from exc
            if existing_bytes != manifest_bytes:
                raise ForgejoError("catalog manifest collision for this upstream release identity")
            branch = self._request(
                "GET",
                f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}"
                f"/branches/{urllib.parse.quote(default_branch, safe='')}",
            )
            commit = branch.get("commit", {}).get("id") or branch.get("commit", {}).get("sha")
            if not isinstance(commit, str) or not commit:
                raise ForgejoError("Forgejo did not return the existing catalog commit")
            return commit
        result = self._request("POST", path, payload, expected=(201,))
        commit = result.get("commit", {}).get("sha")
        if not isinstance(commit, str) or not commit:
            raise ForgejoError("Forgejo did not return the archive catalog commit")
        return commit

    def _paginated(self, path: str) -> list[dict[str, Any]]:
        result = []
        for page in range(1, 202):
            value = self._request("GET", f"{path}?limit=50&page={page}")
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise ForgejoError("Forgejo returned an invalid paginated list")
            result.extend(value)
            if len(result) > 10000:
                raise ForgejoError("Forgejo list exceeds the supported 10000-item bound")
            if len(value) < 50:
                return result
        raise ForgejoError("Forgejo pagination exceeds the supported bound")

    def _list_releases(self, repository: str) -> list[dict[str, Any]]:
        return self._paginated(f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}/releases")

    def _list_assets(self, repository: str, release_id: int) -> list[dict[str, Any]]:
        return self._paginated(f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}/releases/{release_id}/assets")

    def _delete_asset(self, repository: str, release_id: int, asset_id: int) -> None:
        path = (
            f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}"
            f"/releases/{release_id}/assets/{asset_id}"
        )
        self._request("DELETE", path, expected=(204,))

    def _upload_file(self, repository: str, release_id: int, path: Path, name: str) -> dict[str, Any]:
        size = path.stat().st_size
        if size > self.max_attachment_bytes:
            raise ForgejoError(f"attachment {name} exceeds the configured Forgejo attachment limit")
        if self.uploader is not None:
            return self.uploader(repository, release_id, path, name)
        boundary = "rb-" + secrets.token_hex(16)
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="attachment"; filename="{name.replace(chr(34), "_")}"\r\n'
            f"Content-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        query = urllib.parse.urlencode({"name": name})
        api_path = (
            f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}"
            f"/releases/{release_id}/assets?{query}"
        )
        parsed = urllib.parse.urlsplit(self.base_url)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=self.timeout)
        target = (parsed.path.rstrip("/") + api_path) or "/"
        connection.putrequest("POST", target)
        connection.putheader("Authorization", f"token {self.token}")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(prefix) + size + len(suffix)))
        connection.endheaders()
        connection.send(prefix)
        with path.open("rb") as source:
            while block := source.read(64 * 1024):
                connection.send(block)
        connection.send(suffix)
        response = connection.getresponse()
        body = response.read(2 * 1024 * 1024)
        connection.close()
        if response.status != 201:
            raise ForgejoError(f"Forgejo attachment upload returned HTTP {response.status}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ForgejoError("Forgejo upload returned invalid JSON") from exc

    def _stream_attachment_hash(self, url: str, max_bytes: int) -> tuple[int, str]:
        request = urllib.request.Request(self._internal_download_url(url), headers={"Authorization": f"token {self.token}"})
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ForgejoError(f"could not read attachment back: {exc}") from exc
        digest = hashlib.sha256()
        size = 0
        with response:
            if getattr(response, "status", 200) != 200:
                raise ForgejoError("could not read attachment back")
            while block := response.read(64 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise ForgejoError("read-back attachment exceeds expected size")
                digest.update(block)
        return size, digest.hexdigest()

    def publish(
        self,
        upstream_owner: str,
        upstream_repository: str,
        release: Any,
        files: Iterable[Any],
        manifest_path: str | Path,
    ) -> Publication:
        repository = archive_repository_name(upstream_owner, upstream_repository)
        manifest_path = Path(manifest_path)
        inputs = list(files) + [
            type("ManifestFile", (), {
                "path": manifest_path,
                "stored_name": "manifest.json",
                "size": manifest_path.stat().st_size,
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            })()
        ]
        for item in inputs:
            if item.size > self.max_attachment_bytes:
                raise ForgejoError(f"attachment {item.stored_name} exceeds the configured Forgejo attachment limit")
        repo = self.ensure_repository(repository, f"Release archive catalog for https://github.com/{upstream_owner}/{upstream_repository}")
        default_branch = repo.get("default_branch") or "main"
        tag = f"github-release-{release.id}"
        existing = next((item for item in self._list_releases(repository) if item.get("tag_name") == tag), None)
        if existing is None:
            target = self._catalog_commit(repository, release.id, manifest_path, default_branch)
            existing = self._request(
                "POST",
                f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}/releases",
                {
                    "tag_name": tag,
                    "target_commitish": target,
                    "name": f"{release.tag} — archived release",
                    "body": "Archive copy; the catalog commit is not upstream Git history.",
                    "draft": True,
                    "prerelease": False,
                },
                expected=(201,),
            )
        release_id = existing.get("id")
        if not isinstance(release_id, int):
            raise ForgejoError("Forgejo release has no numeric ID")
        expected = {item.stored_name: item for item in inputs}
        listed = self._list_assets(repository, release_id)
        immutable = existing.get("draft") is False
        if immutable:
            if {a.get("name") for a in listed} != set(expected):
                raise ForgejoError("completed archive differs; preserving all existing files")
            for attachment in listed:
                wanted = expected[attachment["name"]]
                size, checksum = self._stream_attachment_hash(attachment["browser_download_url"], wanted.size)
                if size != wanted.size or checksum != wanted.sha256:
                    raise ForgejoError("completed archive checksum differs; preserving existing backup")
            release_url = existing.get("html_url")
            if not isinstance(release_url, str) or not self._public_origin_url(release_url):
                raise ForgejoError("Forgejo returned an invalid release URL")
            return Publication(repository, release_id, release_url, tag)
        for attachment in listed:
            name = attachment.get("name")
            wanted = expected.get(name)
            if wanted is None or attachment.get("size") != wanted.size:
                asset_id = attachment.get("id")
                if not isinstance(asset_id, int):
                    raise ForgejoError("Forgejo attachment has no numeric ID")
                self._delete_asset(repository, release_id, asset_id)
        present = {item.get("name") for item in self._list_assets(repository, release_id)}
        try:
            for name, item in expected.items():
                if name not in present:
                    self._upload_file(repository, release_id, Path(item.path), name)
            verified_assets = self._list_assets(repository, release_id)
            by_name = {item.get("name"): item for item in verified_assets}
            if set(by_name) != set(expected):
                raise ForgejoError("Forgejo attachment list is incomplete")
            for name, item in expected.items():
                attachment = by_name[name]
                if attachment.get("size") != item.size:
                    raise ForgejoError(f"Forgejo attachment size mismatch for {name}")
                download_url = attachment.get("browser_download_url")
                if not isinstance(download_url, str):
                    raise ForgejoError("Forgejo attachment has no read-back URL")
                size, checksum = self._stream_attachment_hash(download_url, item.size)
                if size != item.size or checksum != item.sha256:
                    asset_id = attachment.get("id")
                    if isinstance(asset_id, int):
                        self._delete_asset(repository, release_id, asset_id)
                    raise ForgejoError(f"Forgejo attachment checksum mismatch for {name}")
        except Exception:
            # The draft is intentionally retained for an idempotent repair on retry.
            raise
        updated = self._request(
            "PATCH",
            f"/api/v1/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}/releases/{release_id}",
            {"draft": False, "prerelease": False, "tag_name": tag, "name": f"{release.tag} — archived release"},
        )
        release_url = updated.get("html_url") or existing.get("html_url")
        if not isinstance(release_url, str) or not self._public_origin_url(release_url):
            raise ForgejoError("Forgejo returned an invalid release URL")
        return Publication(repository, release_id, release_url, tag)
