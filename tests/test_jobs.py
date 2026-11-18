import tempfile
import threading
import unittest
from pathlib import Path

from repository_backup.catalog import Catalog
from repository_backup.jobs import JobWorker


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(Path(self.temp.name) / "state.sqlite3")
        self.repository = self.catalog.add_repository("octo", "demo", "https://github.com/octo/demo")

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_requests_do_not_fill_queue_or_fail_existing_job(self):
        worker=JobWorker(self.catalog,lambda *args:None,capacity=1)
        first=worker.submit(self.repository,"release-7")
        for _ in range(10):
            self.assertEqual(first["id"],worker.submit(self.repository,"release-7")["id"])
        self.assertEqual(1,worker.queue.qsize())
        self.assertEqual("queued",self.catalog.get_job(first["id"])["state"])

    def test_recovery_drains_backlog_larger_than_queue_capacity(self):
        jobs=[self.catalog.create_job(self.repository,"recovered-"+str(i)) for i in range(4)]
        observed=[]
        def processor(job,advance,check):
            observed.append(job["id"])
            for state in ("resolving","downloading","verifying","staged","publishing"):
                advance(state)
        worker=JobWorker(self.catalog,processor,capacity=1)
        worker.start();worker.queue.join();worker.stop()
        self.assertEqual([j["id"] for j in jobs],observed)
        self.assertTrue(all(self.catalog.get_job(j["id"])["state"]=="complete" for j in jobs))

    def test_worker_advances_and_completes(self):
        observed = []
        def processor(job, advance, check):
            for state in ("resolving", "downloading", "verifying", "staged", "publishing"):
                advance(state)
                observed.append(state)
        worker = JobWorker(self.catalog, processor)
        worker.start()
        job = worker.submit(self.repository, "one")
        worker.queue.join()
        worker.stop()
        self.assertEqual("complete", self.catalog.get_job(job["id"])["state"])
        self.assertEqual("publishing", observed[-1])

    def test_failure_is_not_complete(self):
        def processor(job, advance, check):
            advance("resolving")
            raise RuntimeError("boom")
        worker = JobWorker(self.catalog, processor)
        worker.start()
        job = worker.submit(self.repository, "failure")
        worker.queue.join()
        worker.stop()
        current = self.catalog.get_job(job["id"])
        self.assertEqual("failed", current["state"])
        self.assertIn("boom", current["error"])

    def test_cancel_requested_is_honoured(self):
        entered = threading.Event()
        release = threading.Event()
        def processor(job, advance, check):
            advance("resolving")
            entered.set()
            release.wait(2)
            check()
        worker = JobWorker(self.catalog, processor)
        worker.start()
        job = worker.submit(self.repository, "cancel")
        self.assertTrue(entered.wait(2))
        worker.cancel(job["id"])
        release.set()
        worker.queue.join()
        worker.stop()
        self.assertEqual("cancelled", self.catalog.get_job(job["id"])["state"])


if __name__ == "__main__":
    unittest.main()
