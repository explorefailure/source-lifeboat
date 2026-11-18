"""SSRF-resistant, bounded streaming downloader for release files."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import re
import shutil
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
})


class DownloadError(RuntimeError):
    pass


class QuotaExceeded(DownloadError):
    pass


@dataclass(frozen=True)
class DownloadedFile:
    kind: str
    original_filename: str
    stored_name: str
    path: Path
    size: int
    sha256: str
    upstream_asset_id: int | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to validated numeric IPs while keeping hostname TLS checks."""
    def connect(self) -> None:
        addresses = list(_default_resolver(self.host))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise OSError("download host resolved to a private or local address")
        if self.host not in ALLOWED_HOSTS or self.port != 443 or self._tunnel_host:
            raise OSError("download connection target is not allowed")
        last_error = None
        for address in addresses:
            sock = None
            try:
                sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_error = exc
        raise OSError("could not connect to a validated download address") from last_error

class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PinnedHTTPSConnection, request, context=self._context)


def safe_filename(name: str, used: set[str] | None = None) -> str:
    used = used if used is not None else set()
    normalized = unicodedata.normalize("NFKC", name)
    normalized = "".join("_" if unicodedata.category(ch).startswith("C") else ch for ch in normalized)
    normalized = normalized.replace("/", "_").replace("\\", "_")
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = re.sub(r"[^A-Za-z0-9._ +()\[\]-]", "_", normalized)
    if normalized in ("", ".", ".."):
        normalized = "download"
    normalized = normalized[:180].rstrip(" .") or "download"
    stem, suffix = os.path.splitext(normalized)
    candidate = normalized
    counter = 2
    while candidate.casefold() in used:
        tail = f"-{counter}"
        candidate = f"{stem[: max(1, 180-len(suffix)-len(tail))]}{tail}{suffix}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def _default_resolver(host: str) -> Iterable[str]:
    return {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}


class SafeDownloader:
    def __init__(
        self,
        destination: str | Path,
        *,
        token: str | None = None,
        max_file_bytes: int = 2 * 1024**3,
        max_total_bytes: int = 10 * 1024**3,
        min_free_bytes: int = 64 * 1024**2,
        max_redirects: int = 5,
        timeout: float = 30.0,
        total_time_limit: float = 3600.0,
        opener: Any = None,
        resolver: Callable[[str], Iterable[str]] = _default_resolver,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)
        self.token = token or None
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes
        self.max_redirects = max_redirects
        self.timeout = timeout
        self.total_time_limit = total_time_limit
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), _PinnedHTTPSHandler())
        self.resolver = resolver
        self.clock = clock
        self.total_downloaded = 0
        self.started = self.clock()

    def _validate_url(self, url: str) -> urllib.parse.SplitResult:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise DownloadError("invalid download URL") from exc
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise DownloadError("download URL is not on an allowed GitHub host")
        try:
            addresses = list(self.resolver(host))
        except OSError as exc:
            raise DownloadError("download host could not be resolved") from exc
        if not addresses:
            raise DownloadError("download host has no address")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise DownloadError("download host resolved to an invalid address") from exc
            if not ip.is_global:
                raise DownloadError("download host resolved to a private or local address")
        return parsed

    def _open(self, url: str, *, asset: bool) -> tuple[Any, str]:
        original_host = self._validate_url(url).hostname
        current = url
        for redirect_count in range(self.max_redirects + 1):
            parsed = self._validate_url(current)
            accept = "application/octet-stream" if asset or parsed.hostname != "api.github.com" else "application/vnd.github+json"
            headers = {"Accept": accept, "User-Agent": "repository-backup/1"}
            if self.token and parsed.hostname == "api.github.com" and parsed.hostname == original_host:
                headers["Authorization"] = f"Bearer {self.token}"
            request = urllib.request.Request(current, headers=headers)
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    response = exc
                else:
                    raise DownloadError(f"download returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise DownloadError(f"download failed: {exc}") from exc
            status = getattr(response, "status", getattr(response, "code", 200))
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise DownloadError("redirect has no location")
                if redirect_count >= self.max_redirects:
                    raise DownloadError("too many redirects")
                current = urllib.parse.urljoin(current, location)
                continue
            if status != 200:
                response.close()
                raise DownloadError(f"download returned HTTP {status}")
            return response, current
        raise DownloadError("too many redirects")

    def download(
        self,
        url: str,
        original_filename: str,
        *,
        kind: str,
        used_names: set[str] | None = None,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        upstream_asset_id: int | None = None,
    ) -> DownloadedFile:
        if expected_size is not None and expected_size > self.max_file_bytes:
            raise QuotaExceeded(f"{original_filename} exceeds the per-file quota")
        if expected_size is not None and self.total_downloaded + expected_size > self.max_total_bytes:
            raise QuotaExceeded("release exceeds the total download quota")
        free = shutil.disk_usage(self.destination).free
        required = self.min_free_bytes + (expected_size or self.max_file_bytes)
        if free < required:
            raise QuotaExceeded("not enough free disk space to start download")
        stored_name = safe_filename(original_filename, used_names)
        final_path = self.destination / stored_name
        partial_path = self.destination / (stored_name + ".partial")
        digest = hashlib.sha256()
        size = 0
        response = None
        try:
            response, _ = self._open(url, asset=kind == "asset")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError as exc:
                    raise DownloadError("invalid Content-Length") from exc
                if declared < 0:
                    raise DownloadError("invalid Content-Length")
                if declared > self.max_file_bytes or self.total_downloaded + declared > self.max_total_bytes:
                    raise QuotaExceeded("declared download size exceeds quota")
                if expected_size is not None and declared != expected_size:
                    raise DownloadError("download size changed upstream")
            with partial_path.open("xb") as output:
                while True:
                    if self.clock() - self.started > self.total_time_limit:
                        raise DownloadError("download time limit exceeded")
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > self.max_file_bytes or self.total_downloaded + size > self.max_total_bytes:
                        raise QuotaExceeded("streamed download exceeds quota")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if expected_size is not None and size != expected_size:
                raise DownloadError("download was partial or changed size")
            checksum = digest.hexdigest()
            if expected_sha256 is not None and checksum.lower() != expected_sha256.lower():
                raise DownloadError("download checksum mismatch")
            partial_path.replace(final_path)
            self.total_downloaded += size
            return DownloadedFile(kind, original_filename, stored_name, final_path, size, checksum, upstream_asset_id)
        except FileExistsError as exc:
            raise DownloadError("a partial file already exists; use a fresh staging directory") from exc
        finally:
            if response is not None:
                response.close()
            if partial_path.exists():
                partial_path.unlink()

    def download_release(self, release: Any) -> list[DownloadedFile]:
        # The application-generated manifest is always attached under this name.
        used: set[str] = {"manifest.json"}
        result = [self.download(
            release.source_url,
            f"source-{release.commit_sha}.tar.gz",
            kind="source",
            used_names=used,
        )]
        for asset in release.assets:
            result.append(self.download(
                asset.url,
                asset.name,
                kind="asset",
                used_names=used,
                expected_size=asset.size,
                upstream_asset_id=asset.id,
            ))
        return result
