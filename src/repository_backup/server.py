"""Standard-library HTTP service and real backup pipeline."""

from __future__ import annotations

import hashlib
import hmac
import json
import ipaddress
import os
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .catalog import Catalog, CatalogError
from .downloader import DownloadedFile, SafeDownloader
from .forgejo import ForgejoClient, archive_repository_name
from .github import GitHubClient, GitHubError, NoRelease, parse_repository_url
from .jobs import JobWorker
from . import oauth
from .auth import OwnerAuth
from .manifest import build_manifest, read_manifest, write_manifest, same_release_contents


STATIC = Path(__file__).with_name("static")


@dataclass(frozen=True)
class Config:
    state_dir: Path
    public_url: str
    forgejo_url: str
    forgejo_token: str
    forgejo_owner: str
    installation_id: str
    login_token: str
    max_file_bytes: int = 2 * 1024**3
    max_total_bytes: int = 10 * 1024**3
    forgejo_public_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            Path(os.environ.get("RB_STATE_DIR", "/data")),
            os.environ.get("RB_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/"),
            os.environ.get("RB_FORGEJO_URL", "http://forgejo:3000").rstrip("/"),
            os.environ.get("RB_FORGEJO_TOKEN", ""),
            os.environ.get("RB_FORGEJO_OWNER", "repository-backup"),
            os.environ.get("RB_INSTALLATION_ID", ""),
            os.environ.get("RB_LOGIN_TOKEN", ""),
            int(os.environ.get("RB_MAX_FILE_BYTES", str(2 * 1024**3))),
            int(os.environ.get("RB_MAX_TOTAL_BYTES", str(10 * 1024**3))),
            os.environ.get("RB_FORGEJO_PUBLIC_URL", os.environ.get("RB_FORGEJO_URL", "http://forgejo:3000")).rstrip("/"),
        )

    @property
    def setup_needed(self) -> bool:
        return not all((self.forgejo_token, self.installation_id))

    @property
    def origin(self) -> str:
        parsed = urllib.parse.urlsplit(self.public_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.public_url).netloc


class GitHubSettings:
    """Installation-local credentials. Public status never includes the token."""
    def __init__(self, state_dir: Path):
        self.folder = state_dir / "private"
        if self.folder.is_symlink():
            raise ValueError("private settings directory must not be a symlink")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        self.path = self.folder / "github.json"
        self.lock = threading.RLock()
        self.client_path = self.folder / "github-client.json"
        self.device = oauth.DeviceFlow(self)

    def client_id(self) -> str:
        with self.lock:
            if self.client_path.is_symlink():
                raise ValueError("GitHub client settings must not be a symlink")
            if self.client_path.exists():
                return oauth.client_id(json.loads(self.client_path.read_text())["client_id"])
            return oauth.client_id(os.environ.get("RB_GITHUB_CLIENT_ID", oauth.DEFAULT_CLIENT_ID))

    def token(self) -> str | None:
        with self.lock:
            if self.path.is_symlink():
                raise ValueError("GitHub settings must not be a symlink")
            if self.path.exists():
                self.path.chmod(0o600)
                record = json.loads(self.path.read_text())
                if record.get("kind") == "oauth" and record.get("expires_at", float("inf")) <= time.time() + 60:
                    if record.get("refresh_expires_at", 0) <= time.time():
                        raise GitHubError("GitHub sign-in expired. Reconnect in Settings.")
                    result = oauth.post("/login/oauth/access_token", {"client_id": record["client_id"],
                        "grant_type": "refresh_token", "refresh_token": record["refresh_token"]})
                    if result.get("error"):
                        raise GitHubError("GitHub sign-in could not be refreshed. Reconnect in Settings.")
                    updated = oauth.token_record(result, record["client_id"])
                    if "expires_at" not in updated:
                        raise GitHubError("GitHub returned incomplete refreshed credentials. Reconnect in Settings.")
                    # Revalidate identity on rotation as well as initial sign-in.
                    connection = self.test(updated["token"])
                    updated["account"] = connection["account"]
                    if record.get("account") != updated["account"]:
                        raise GitHubError("GitHub account changed during refresh. Reconnect in Settings.")
                    self.write_private(self.path, updated)
                    record = updated
                return record["token"]
            return os.environ.get("RB_GITHUB_TOKEN") or None

    def status(self) -> dict[str, Any]:
        with self.lock:
            if self.path.is_symlink():
                raise ValueError("GitHub settings must not be a symlink")
            record = json.loads(self.path.read_text()) if self.path.exists() else {"token": os.environ.get("RB_GITHUB_TOKEN")}
            token = record.get("token")
            return {"configured": bool(token), "mode": "authenticated" if token else "anonymous",
                    "source": "settings" if self.path.exists() else "environment" if token else "none",
                    "kind": record.get("kind", "token") if token else "anonymous",
                    "account": record.get("account"), "expires_at": record.get("expires_at"),
                    "client_id": self.client_id()}

    def write_private(self, path: Path, value: dict[str, Any]) -> None:
        if path.is_symlink():
            raise ValueError("GitHub settings must not be a symlink")
        fd, name = tempfile.mkstemp(prefix=".github-", dir=self.folder)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(value, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def save(self, token: str | None) -> None:
        with self.lock:
            self.device.flow = None
            self.write_private(self.path, {"token": token})

    @staticmethod
    def validate_token(token: Any) -> str:
        if not isinstance(token, str) or not 20 <= len(token) <= 255 or not token.isascii() or not all(c.isalnum() or c == "_" for c in token):
            raise ValueError("Enter a GitHub personal access token; use Remove token to return to anonymous mode.")
        return token

    def test(self, token: str | None) -> dict[str, Any]:
        client = GitHubClient(token=token)
        account = client._json("/user").get("login") if token else None
        if token and (not isinstance(account, str) or not 1 <= len(account) <= 39 or not all(c.isascii() and (c.isalnum() or c == "-") for c in account)):
            raise GitHubError("GitHub returned an invalid account response")
        core = client._json("/rate_limit").get("resources", {}).get("core", {})
        quota = {key: core.get(key) for key in ("limit", "remaining", "reset")}
        if any(type(value) is not int for value in quota.values()):
            raise GitHubError("GitHub returned an invalid quota response")
        return {"account": account, "quota": quota, "tested": True}


class DownloadSettings:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.resolve()
        self.path = state_dir / "private" / "downloads.json"
        self.lock = threading.RLock()

    def root(self) -> Path:
        with self.lock:
            if self.path.is_symlink():
                raise ValueError("download settings must not be a symlink")
            if self.path.exists():
                return Path(json.loads(self.path.read_text())["folder"])
            return self.state_dir

    def status(self) -> dict[str, Any]:
        root = self.root()
        return {"folder": str(root), "default_folder": str(self.state_dir), "custom": root != self.state_dir}

    def validate(self, value: Any) -> Path:
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("Enter an absolute folder path on the application server.")
        folder = Path(value).expanduser()
        if not folder.is_absolute():
            raise ValueError("Use an absolute folder path, such as /data/downloads.")
        folder = folder.resolve()
        private = self.state_dir / "private"
        if folder == private or private in folder.parents:
            raise ValueError("Choose a folder outside the private settings directory.")
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.is_dir():
            raise ValueError("The download location must be a folder.")
        for child in (folder / "staging", folder / "snapshots"):
            if child.is_symlink():
                raise ValueError("Download staging and snapshots must not be symlinks.")
            child.mkdir(exist_ok=True)
            with tempfile.TemporaryFile(dir=child):
                pass
        return folder

    def save(self, value: Any) -> dict[str, Any]:
        folder = self.validate(value)
        with self.lock:
            fd, name = tempfile.mkstemp(prefix=".downloads-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump({"folder": str(folder)}, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
        return self.status()


class Application:
    def __init__(self, config: Config, *, start_worker: bool = True):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(config.state_dir / "catalog.sqlite3")
        self.github_settings = GitHubSettings(config.state_dir)
        self.download_settings = DownloadSettings(config.state_dir)
        self.owner_auth = OwnerAuth(config.state_dir, config.login_token)
        self.sessions: dict[str, float] = {}
        self.sessions_lock = threading.Lock()
        self.worker = JobWorker(self.catalog, self._process_job, workers=1, capacity=32)
        if start_worker and not config.setup_needed:
            self.worker.start()

    def close(self) -> None:
        self.worker.stop()

    def _process_job(self, job: dict[str, Any], advance: Any, check_cancelled: Any) -> None:
        repository_row = self.catalog.get_repository(job["repository_id"])
        if not repository_row:
            raise RuntimeError("repository disappeared")
        repository = parse_repository_url(repository_row["github_url"])
        download_root = self.download_settings.validate(str(self.download_settings.root()))
        advance("resolving", {"message": "Resolving latest published release"})
        github_token = self.github_settings.token()
        github = GitHubClient(token=github_token)
        try:
            release = github.latest_release(repository)
        except NoRelease as exc:
            self.catalog.transition_job(job["id"], "no-release", expected="resolving", error=str(exc))
            return
        check_cancelled()
        stage_root = download_root / "staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f"job-{job['id']}-", dir=stage_root))
        try:
            advance("downloading", {"message": "Downloading source archive and release files"})
            downloader = SafeDownloader(
                stage,
                token=github_token,
                max_file_bytes=self.config.max_file_bytes,
                max_total_bytes=self.config.max_total_bytes,
            )
            files = downloader.download_release(release)
            check_cancelled()
            advance("verifying", {"message": "Checking upstream release has not changed"})
            try:
                github.assert_unchanged(repository, release)
            except GitHubError as exc:
                self.catalog.transition_job(job["id"], "stale", expected="verifying", error=str(exc))
                return
            retrieved_at = datetime.now(UTC).isoformat()
            manifest = build_manifest(repository, release, files, retrieved_at=retrieved_at)
            manifest_path = write_manifest(stage / "manifest.json", manifest)
            final = download_root / "snapshots" / archive_repository_name(repository.owner, repository.name) / str(release.id)
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                # A prior staged/published copy is authoritative; do not replace it.
                existing_manifest = read_manifest(final / "manifest.json")
                if not same_release_contents(existing_manifest, manifest):
                    self.catalog.transition_job(job["id"], "stale", expected="verifying", error="Upstream release files differ from the preserved snapshot; existing backup was not changed.")
                    return
                shutil.rmtree(stage)
                identity = existing_manifest.get("release", {})
                if identity.get("github_release_id") != release.id or identity.get("commit_sha") != release.commit_sha:
                    raise RuntimeError("existing staged backup has a conflicting release identity")
                verified_files: list[DownloadedFile] = []
                for item in existing_manifest["files"]:
                    name = item.get("stored_name")
                    if not isinstance(name, str) or Path(name).name != name:
                        raise RuntimeError("existing staged backup has an unsafe filename")
                    path = final / name
                    digest = hashlib.sha256()
                    size = 0
                    with path.open("rb") as source:
                        while block := source.read(64 * 1024):
                            size += len(block)
                            digest.update(block)
                    if size != item.get("size") or digest.hexdigest() != item.get("sha256"):
                        raise RuntimeError(f"existing staged backup failed integrity verification: {name}")
                    verified_files.append(DownloadedFile(
                        item["kind"], item["original_filename"], name, path, size,
                        digest.hexdigest(), item.get("upstream_asset_id"),
                    ))
                files = verified_files
            else:
                stage.replace(final)
                files = [DownloadedFile(
                    item.kind, item.original_filename, item.stored_name,
                    final / item.stored_name, item.size, item.sha256, item.upstream_asset_id,
                ) for item in files]
            manifest_path = final / "manifest.json"
            snapshot_id = self.catalog.upsert_snapshot(
                job["repository_id"], release.id, release.tag, release.commit_sha,
                release.source_url, retrieved_at,
                {"html_url": release.html_url, "published_at": release.published_at},
            )
            for item in files:
                self.catalog.add_artifact(
                    snapshot_id, item.kind, item.original_filename, item.stored_name,
                    item.size, item.sha256, str(item.path), item.upstream_asset_id,
                )
            manifest_bytes = manifest_path.read_bytes()
            self.catalog.add_artifact(
                snapshot_id, "manifest", "manifest.json", "manifest.json", len(manifest_bytes),
                hashlib.sha256(manifest_bytes).hexdigest(), str(manifest_path), None,
            )
            self.catalog.transition_job(job["id"], "staged", expected="verifying", snapshot_id=snapshot_id)
            check_cancelled()
            self.catalog.transition_job(job["id"], "publishing", expected="staged", progress={"message": "Publishing to dedicated Forgejo"})
            self.catalog.set_publication(snapshot_id, self.config.forgejo_owner, archive_repository_name(repository.owner, repository.name), "uploading")
            forgejo = ForgejoClient(
                self.config.forgejo_url, self.config.forgejo_token, self.config.forgejo_owner,
                public_url=self.config.forgejo_public_url or self.config.forgejo_url,
                max_attachment_bytes=self.config.max_file_bytes,
            )
            try:
                publication = forgejo.publish(repository.owner, repository.name, release, files, manifest_path)
            except Exception as exc:
                self.catalog.set_publication(
                    snapshot_id, self.config.forgejo_owner, archive_repository_name(repository.owner, repository.name),
                    "failed", error=str(exc)[:2000],
                )
                raise
            self.catalog.set_publication(
                snapshot_id, self.config.forgejo_owner, publication.repository, "verified",
                release_id=publication.release_id, release_url=publication.release_url,
            )
            self.catalog.transition_job(job["id"], "complete", expected="publishing", progress={"message": "Backup verified"})
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "SourceLifeboat/1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> Application:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        super().log_message(format, *args)

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def _cookies(self) -> SimpleCookie[str]:
        cookies: SimpleCookie[str] = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        return cookies

    def _valid_host(self) -> bool:
        return hmac.compare_digest(self.headers.get("Host", ""), self.app.config.host)

    def _valid_origin(self) -> bool:
        return hmac.compare_digest(self.headers.get("Origin", ""), self.app.config.origin)

    def _authenticated(self) -> bool:
        session = self._cookies().get("rb_session")
        if not session:
            return False
        with self.app.sessions_lock:
            expires = self.app.sessions.get(session.value, 0)
            if expires < time.time():
                self.app.sessions.pop(session.value, None)
                return False
            return True

    def _csrf_valid(self) -> bool:
        cookie = self._cookies().get("rb_csrf")
        supplied = self.headers.get("X-CSRF-Token", "")
        return bool(cookie and supplied and hmac.compare_digest(cookie.value, supplied))

    def _guard(self, *, auth: bool = False, state_change: bool = False) -> bool:
        if not self._valid_host():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unrecognised Host"})
            return False
        if state_change and (not self._valid_origin() or not self._csrf_valid()):
            self._json(HTTPStatus.FORBIDDEN, {"error": "Origin or CSRF check failed"})
            return False
        if auth and not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
            return False
        return True

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return None
        if length < 0 or length > 64 * 1024:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body is too large"})
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return None
        if not isinstance(value, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
            return None
        return value

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/status":
            csrf = self._cookies().get("rb_csrf")
            token = csrf.value if csrf else secrets.token_urlsafe(32)
            body = json.dumps({
                "setup_needed": self.app.config.setup_needed,
                "setup_message": "Configure the dedicated Forgejo and installation identity to enable backups." if self.app.config.setup_needed else None,
                "authenticated": self._authenticated(),
                "owner_setup_needed": not self.app.owner_auth.exists(),
                "owner_setup_key_required": bool(self.app.config.login_token or os.environ.get("RB_SETUP_TOKEN")),
                "installation_id": self.app.config.installation_id or None,
                "csrf_token": token,
            }, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self._security_headers()
            self.send_header("Set-Cookie", f"rb_csrf={token}; Path=/; SameSite=Strict" + ("; Secure" if self.app.config.origin.startswith("https://") else ""))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if path == "/api/settings/downloads":
            if not self._guard(auth=True):
                return
            self._json(200, self.app.download_settings.status())
            return
        if path == "/api/settings/github":
            if not self._guard(auth=True):
                return
            self._json(200, self.app.github_settings.status())
            return
        if path == "/api/jobs":
            if not self._authenticated():
                self._json(401, {"error": "authentication required"})
                return
            self._json(200, {"jobs": self._job_views(self.app.catalog.list_jobs())})
            return
        if path.startswith("/api/jobs/"):
            if not self._authenticated():
                self._json(401, {"error": "authentication required"})
                return
            try:
                job_id = int(path.removeprefix("/api/jobs/"))
            except ValueError:
                self._json(404, {"error": "not found"})
                return
            job = self.app.catalog.get_job(job_id)
            if not job:
                self._json(404, {"error": "job not found"})
                return
            self._json(200, self._job_views([job])[0])
            return
        static_name = "index.html" if path in ("/", "/settings") else path.removeprefix("/static/") if path.startswith("/static/") else ""
        if static_name in {"index.html", "style.css", "app.js"}:
            content_type = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}[Path(static_name).suffix]
            self._send(200, (STATIC / static_name).read_bytes(), content_type)
            return
        self._json(404, {"error": "not found"})

    def _job_views(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for job in jobs:
            view = {key: job.get(key) for key in ("id", "state", "error", "progress", "created_at", "updated_at")}
            repo = self.app.catalog.get_repository(job["repository_id"])
            view["repository"] = repo["github_url"] if repo else None
            if job.get("snapshot_id"):
                snapshot = self.app.catalog.get_snapshot(job["snapshot_id"])
                view["snapshot"] = snapshot
                view["files"] = [
                    {key: item[key] for key in ("kind", "original_filename", "stored_name", "size", "sha256", "upstream_asset_id")}
                    for item in self.app.catalog.list_artifacts(job["snapshot_id"])
                ]
                view["publication"] = self.app.catalog.get_publication(job["snapshot_id"])
            output.append(view)
        return output

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._guard(state_change=True):
            return
        value = self._read_json()
        if value is None:
            return
        if path in ("/api/login", "/api/setup/owner"):
            if path == "/api/setup/owner":
                if self.app.owner_auth.exists():
                    self._json(409, {"error": "An owner password is already configured. Sign in with it."})
                    return
                setup_key = os.environ.get("RB_SETUP_TOKEN") or self.app.config.login_token
                if setup_key:
                    supplied = value.get("setup_key", "")
                    if not self._authenticated() and (not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), setup_key.encode())):
                        self._json(403, {"error": "Enter the existing installation setup key to create the owner password."})
                        return
                else:
                    # An unclaimed installation can only be initialized locally.
                    try:
                        local_peer = ipaddress.ip_address(self.client_address[0]).is_loopback
                        local_url = ipaddress.ip_address(urllib.parse.urlsplit(self.app.config.public_url).hostname).is_loopback
                    except ValueError:
                        local_peer = local_url = False
                    if not (local_peer and local_url):
                        self._json(403, {"error": "First setup requires a local connection, or an operator-configured RB_SETUP_TOKEN."})
                        return
                try:
                    self.app.owner_auth.create(value.get("password"), value.get("confirmation"))
                except FileExistsError:
                    self._json(409, {"error": "An owner password is already configured."})
                    return
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                except OSError:
                    self._json(500, {"error": "Could not save the owner password. Check server storage permissions."})
                    return
                with self.app.sessions_lock:
                    self.app.sessions.clear()
            else:
                try:
                    valid = self.app.owner_auth.verify(value.get("password", value.get("token")))
                except ValueError as exc:
                    self._json(429, {"error": str(exc)})
                    return
                if not valid:
                    self._json(401, {"error": "Incorrect owner password."})
                    return
            session = secrets.token_urlsafe(32)
            with self.app.sessions_lock:
                self.app.sessions[session] = time.time() + 12 * 3600
            body = b'{"ok":true}'
            self.send_response(200)
            self._security_headers()
            self.send_header("Set-Cookie", f"rb_session={session}; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.app.config.origin.startswith("https://") else ""))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authenticated():
            self._json(401, {"error": "authentication required"})
            return
        if path == "/api/settings/downloads":
            try:
                self._json(200, self.app.download_settings.save(value.get("folder")))
            except (ValueError, OSError):
                self._json(400, {"error": "Choose an absolute, writable server folder outside private settings; staging and snapshots must not be symlinks. Existing settings were kept."})
            return
        if path == "/api/settings/github/device":
            try:
                session = self._cookies()["rb_session"].value
                result = self.app.github_settings.device.action(value.get("action"), value, session)
                self._json(200, result)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except GitHubError as exc:
                self._json(502, {"error": str(exc)})
            except OSError:
                self._json(500, {"error": "Could not store GitHub sign-in. Check server storage permissions."})
            return
        if path == "/api/settings/github":
            action = value.get("action")
            settings = self.app.github_settings
            try:
                if action == "save":
                    token = settings.validate_token(value.get("token"))
                    with settings.lock:
                        connection = settings.test(token)
                        settings.save(token)
                    self._json(200, {**settings.status(), **connection})
                elif action == "remove":
                    settings.save(None)
                    self._json(200, settings.status())
                elif action == "test":
                    self._json(200, {**settings.status(), **settings.test(settings.token())})
                else:
                    self._json(400, {"error": "Choose save, test or remove."})
            except ValueError:
                self._json(400, {"error": "Enter a valid GitHub personal access token."})
            except GitHubError:
                self._json(502, {"error": "GitHub connection failed. Check the token, its expiry and GitHub's API quota. Existing settings were kept."})
            except OSError:
                self._json(500, {"error": "Could not store GitHub settings. Check the service data-directory permissions."})
            return
        if path in ("/api/repositories", "/api/check"):
            url = value.get("url")
            if not isinstance(url, str):
                self._json(400, {"error": "url is required"})
                return
            try:
                repository = parse_repository_url(url)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            repository_id = self.app.catalog.add_repository(repository.owner, repository.name, repository.url)
            try:
                release = GitHubClient(token=self.app.github_settings.token()).latest_release(repository)
                identity = [release.id, release.tag, release.commit_sha, [(a.id, a.name, a.size) for a in release.assets]]
                upstream_key = "release-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            except NoRelease:
                upstream_key = "no-release"
            except GitHubError as exc:
                self._json(502, {"error": str(exc)})
                return
            job = self.app.worker.submit(repository_id, upstream_key)
            self._json(202, {"job": self._job_views([job])[0]})
            return
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            raw = path.removeprefix("/api/jobs/").removesuffix("/cancel")
            try:
                job = self.app.worker.cancel(int(raw))
            except (ValueError, CatalogError) as exc:
                self._json(404, {"error": str(exc)})
                return
            self._json(200, {"job": self._job_views([job])[0]})
            return
        self._json(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: Application):
        self.app = app
        super().__init__(address, Handler)


def main() -> None:
    config = Config.from_env()
    app = Application(config)
    server = Server((os.environ.get("RB_BIND", "0.0.0.0"), int(os.environ.get("RB_PORT", "8080"))), app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
