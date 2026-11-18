import tempfile
import unittest
from pathlib import Path

from repository_backup.catalog import Catalog, CatalogError


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(Path(self.temp.name) / "catalog.sqlite3")
        self.repository = self.catalog.add_repository("octo", "demo", "https://github.com/octo/demo")

    def tearDown(self):
        self.temp.cleanup()

    def test_deduplicates_repositories_snapshots_artifacts_and_jobs(self):
        self.assertEqual(self.repository, self.catalog.add_repository("octo", "demo", "https://github.com/octo/demo"))
        first = self.catalog.upsert_snapshot(self.repository, 10, "v1", "a" * 40, "https://api.github.com/source", "now")
        second = self.catalog.upsert_snapshot(self.repository, 10, "v1", "a" * 40, "https://api.github.com/source", "later")
        self.assertEqual(first, second)
        self.catalog.add_artifact(first, "source", "source.tar.gz", "source.tar.gz", 3, "a" * 64, "/tmp/a")
        self.catalog.add_artifact(first, "source", "source.tar.gz", "source.tar.gz", 3, "a" * 64, "/tmp/a")
        self.assertEqual(1, len(self.catalog.list_artifacts(first)))
        self.assertEqual(self.catalog.create_job(self.repository)["id"], self.catalog.create_job(self.repository)["id"])

    def test_state_retry_and_restart_preserve_complete_history(self):
        old = self.catalog.upsert_snapshot(self.repository, 9, "v0", "b" * 40, "https://api.github.com/old", "old")
        job = self.catalog.create_job(self.repository, "release-9")
        self.catalog.transition_job(job["id"], "resolving", expected="queued")
        self.catalog.transition_job(job["id"], "complete", expected="resolving", snapshot_id=old)
        active = self.catalog.create_job(self.repository, "release-10")
        self.catalog.transition_job(active["id"], "downloading", expected="queued")
        self.assertEqual(1, self.catalog.recover_interrupted_jobs())
        self.assertEqual("queued", self.catalog.get_job(active["id"])["state"])
        self.assertEqual("complete", self.catalog.get_job(job["id"])["state"])
        self.catalog.transition_job(active["id"], "failed", expected="queued", error="partial")
        self.assertEqual("queued", self.catalog.retry_job(active["id"])["state"])
        with self.assertRaises(CatalogError):
            self.catalog.retry_job(job["id"])

    def test_older_completed_backups_remain(self):
        for release_id in (1, 2):
            self.catalog.upsert_snapshot(self.repository, release_id, f"v{release_id}", str(release_id) * 40, "https://api.github.com/source", "now")
        self.assertEqual([2, 1], [item["github_release_id"] for item in self.catalog.list_snapshots(self.repository)])


if __name__ == "__main__":
    unittest.main()
