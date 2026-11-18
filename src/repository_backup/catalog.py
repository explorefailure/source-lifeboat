"""Durable SQLite catalog for repositories, release snapshots and jobs."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator


ACTIVE_STATES = ("queued", "resolving", "downloading", "verifying", "staged", "publishing")
TERMINAL_STATES = ("complete", "failed", "cancelled", "stale", "no-release")
ALL_STATES = ACTIVE_STATES + TERMINAL_STATES


class CatalogError(RuntimeError):
    pass


class Catalog:
    """Small transactional catalog. One connection is opened per operation."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.Lock()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._migration_lock:
            db = self._connect()
            try:
                db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
                version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
                if version < 1:
                    db.executescript(
                    """
                    CREATE TABLE repositories (
                        id INTEGER PRIMARY KEY,
                        owner TEXT NOT NULL,
                        name TEXT NOT NULL,
                        github_url TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(owner, name), UNIQUE(github_url)
                    );
                    CREATE TABLE release_snapshots (
                        id INTEGER PRIMARY KEY,
                        repository_id INTEGER NOT NULL REFERENCES repositories(id),
                        github_release_id INTEGER NOT NULL,
                        tag TEXT NOT NULL,
                        commit_sha TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        retrieved_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(repository_id, github_release_id)
                    );
                    CREATE TABLE artifacts (
                        id INTEGER PRIMARY KEY,
                        snapshot_id INTEGER NOT NULL REFERENCES release_snapshots(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL CHECK(kind IN ('source', 'asset', 'manifest')),
                        original_filename TEXT NOT NULL,
                        stored_name TEXT NOT NULL,
                        size INTEGER NOT NULL CHECK(size >= 0),
                        sha256 TEXT NOT NULL,
                        upstream_asset_id INTEGER,
                        local_path TEXT NOT NULL,
                        UNIQUE(snapshot_id, stored_name),
                        UNIQUE(snapshot_id, upstream_asset_id)
                    );
                    CREATE TABLE publications (
                        id INTEGER PRIMARY KEY,
                        snapshot_id INTEGER NOT NULL REFERENCES release_snapshots(id),
                        forgejo_owner TEXT NOT NULL,
                        forgejo_repository TEXT NOT NULL,
                        forgejo_release_id INTEGER,
                        forgejo_release_url TEXT,
                        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','uploading','verified','failed')),
                        verified_at TEXT,
                        error TEXT,
                        UNIQUE(snapshot_id)
                    );
                    CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY,
                        repository_id INTEGER NOT NULL REFERENCES repositories(id),
                        snapshot_id INTEGER REFERENCES release_snapshots(id),
                        upstream_key TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('queued','resolving','downloading','verifying','staged','publishing','complete','failed','cancelled','stale','no-release')),
                        attempt INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(repository_id, upstream_key)
                    );
                    CREATE INDEX jobs_state_idx ON jobs(state, id);
                    INSERT INTO schema_migrations(version) VALUES (1);
                    """
                    )
            finally:
                db.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        if "progress_json" in value:
            value["progress"] = json.loads(value.pop("progress_json") or "{}")
        if "metadata_json" in value:
            value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
        return value

    def add_repository(self, owner: str, name: str, github_url: str) -> int:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO repositories(owner,name,github_url) VALUES(?,?,?) "
                "ON CONFLICT(owner,name) DO UPDATE SET github_url=excluded.github_url",
                (owner, name, github_url),
            )
            return int(db.execute("SELECT id FROM repositories WHERE owner=? AND name=?", (owner, name)).fetchone()[0])

    def get_repository(self, repository_id: int) -> dict[str, Any] | None:
        with self.connection() as db:
            return self._dict(db.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone())

    def list_repositories(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [self._dict(row) for row in db.execute("SELECT * FROM repositories ORDER BY owner,name")]

    def upsert_snapshot(
        self,
        repository_id: int,
        github_release_id: int,
        tag: str,
        commit_sha: str,
        source_url: str,
        retrieved_at: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO release_snapshots
                   (repository_id,github_release_id,tag,commit_sha,source_url,retrieved_at,metadata_json)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(repository_id,github_release_id) DO NOTHING""",
                (repository_id, github_release_id, tag, commit_sha, source_url, retrieved_at, json.dumps(metadata or {}, sort_keys=True)),
            )
            row = db.execute(
                "SELECT id,tag,commit_sha,source_url FROM release_snapshots WHERE repository_id=? AND github_release_id=?",
                (repository_id, github_release_id),
            ).fetchone()
            if (row["tag"], row["commit_sha"], row["source_url"]) != (tag, commit_sha, source_url):
                raise CatalogError("upstream release identity conflicts with an existing immutable snapshot")
            return int(row["id"])

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self.connection() as db:
            return self._dict(db.execute("SELECT * FROM release_snapshots WHERE id=?", (snapshot_id,)).fetchone())

    def list_snapshots(self, repository_id: int) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [self._dict(row) for row in db.execute(
                "SELECT * FROM release_snapshots WHERE repository_id=? ORDER BY id DESC", (repository_id,)
            )]

    def add_artifact(
        self,
        snapshot_id: int,
        kind: str,
        original_filename: str,
        stored_name: str,
        size: int,
        sha256: str,
        local_path: str,
        upstream_asset_id: int | None = None,
    ) -> int:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO artifacts
                   (snapshot_id,kind,original_filename,stored_name,size,sha256,upstream_asset_id,local_path)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_id,stored_name) DO NOTHING""",
                (snapshot_id, kind, original_filename, stored_name, size, sha256, upstream_asset_id, local_path),
            )
            row = db.execute(
                "SELECT * FROM artifacts WHERE snapshot_id=? AND stored_name=?", (snapshot_id, stored_name)
            ).fetchone()
            expected = (kind, original_filename, size, sha256, upstream_asset_id)
            actual = (row["kind"], row["original_filename"], row["size"], row["sha256"], row["upstream_asset_id"])
            if actual != expected:
                raise CatalogError("artifact identity conflicts with an existing immutable snapshot")
            return int(row["id"])

    def list_artifacts(self, snapshot_id: int) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM artifacts WHERE snapshot_id=? ORDER BY kind, id", (snapshot_id,)
            )]

    def create_job(self, repository_id: int, upstream_key: str = "latest") -> dict[str, Any]:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO jobs(repository_id,upstream_key,state) VALUES(?,?,'queued') "
                "ON CONFLICT(repository_id,upstream_key) DO NOTHING",
                (repository_id, upstream_key),
            )
            row = db.execute(
                "SELECT * FROM jobs WHERE repository_id=? AND upstream_key=?", (repository_id, upstream_key)
            ).fetchone()
            return self._dict(row)  # type: ignore[return-value]

    def retry_job(self, job_id: int) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise CatalogError("job not found")
            if row[0] not in ("failed", "cancelled", "stale", "no-release"):
                raise CatalogError("only an unsuccessful terminal job can be retried")
            db.execute(
                "UPDATE jobs SET state='queued',attempt=attempt+1,error=NULL,cancel_requested=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connection() as db:
            return self._dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [self._dict(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
            )]

    def next_queued_job(self) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            return self._dict(row)

    def transition_job(
        self,
        job_id: int,
        state: str,
        *,
        expected: str | tuple[str, ...] | None = None,
        error: str | None = None,
        snapshot_id: int | None = None,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in ALL_STATES:
            raise CatalogError(f"invalid job state: {state}")
        expected_states = (expected,) if isinstance(expected, str) else expected
        with self.transaction() as db:
            row = db.execute("SELECT state,cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise CatalogError("job not found")
            if expected_states and row[0] not in expected_states:
                raise CatalogError(f"job is {row[0]}, expected {', '.join(expected_states)}")
            if row[1] and state not in TERMINAL_STATES:
                state = "cancelled"
            fields = ["state=?", "error=?", "updated_at=CURRENT_TIMESTAMP"]
            values: list[Any] = [state, error]
            if snapshot_id is not None:
                fields.append("snapshot_id=?")
                values.append(snapshot_id)
            if progress is not None:
                fields.append("progress_json=?")
                values.append(json.dumps(progress, sort_keys=True))
            values.append(job_id)
            db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)
        return self.get_job(job_id)  # type: ignore[return-value]

    def request_cancel(self, job_id: int) -> dict[str, Any]:
        with self.transaction() as db:
            result = db.execute(
                "UPDATE jobs SET cancel_requested=1, updated_at=CURRENT_TIMESTAMP WHERE id=? AND state IN (%s)"
                % ",".join("?" for _ in ACTIVE_STATES),
                (job_id, *ACTIVE_STATES),
            )
            if result.rowcount == 0 and db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
                raise CatalogError("job not found")
        return self.get_job(job_id)  # type: ignore[return-value]

    def recover_interrupted_jobs(self, *, resume: bool = True) -> int:
        """Recover active work after restart; complete rows are never altered."""
        with self.transaction() as db:
            target = "queued" if resume else "failed"
            error = "resumed after interrupted process" if resume else "interrupted by process restart"
            result = db.execute(
                "UPDATE jobs SET state=?,error=?,attempt=attempt+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE state IN (%s)" % ",".join("?" for _ in ACTIVE_STATES[1:]),
                (target, error, *ACTIVE_STATES[1:]),
            )
            return int(result.rowcount)

    def set_publication(
        self,
        snapshot_id: int,
        forgejo_owner: str,
        forgejo_repository: str,
        state: str,
        *,
        release_id: int | None = None,
        release_url: str | None = None,
        error: str | None = None,
    ) -> int:
        if state not in ("pending", "uploading", "verified", "failed"):
            raise CatalogError("invalid publication state")
        verified = "CURRENT_TIMESTAMP" if state == "verified" else "NULL"
        with self.transaction() as db:
            db.execute(
                f"""INSERT INTO publications
                    (snapshot_id,forgejo_owner,forgejo_repository,forgejo_release_id,forgejo_release_url,state,verified_at,error)
                    VALUES(?,?,?,?,?,?,{verified},?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                      forgejo_release_id=excluded.forgejo_release_id,
                      forgejo_release_url=excluded.forgejo_release_url,
                      state=excluded.state,verified_at={verified},error=excluded.error""",
                (snapshot_id, forgejo_owner, forgejo_repository, release_id, release_url, state, error),
            )
            return int(db.execute("SELECT id FROM publications WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0])

    def get_publication(self, snapshot_id: int) -> dict[str, Any] | None:
        with self.connection() as db:
            return self._dict(db.execute("SELECT * FROM publications WHERE snapshot_id=?", (snapshot_id,)).fetchone())
