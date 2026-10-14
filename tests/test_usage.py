import concurrent.futures
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from fitmix.services.context import ServiceError
from fitmix.services.usage import SCHEMA_SQL, UsagePolicy, UsageService, backfill_legacy_usage


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "budget.sqlite3"
        with self.connection() as connection:
            connection.executescript(SCHEMA_SQL)
        self.service = UsageService(self.connection)

    def tearDown(self):
        self.temporary.cleanup()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def assert_code(self, code, call):
        with self.assertRaises(ServiceError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_concurrent_global_limit_and_duplicate_reservation(self):
        service = UsageService(self.connection, UsagePolicy(global_limit_cents=60))
        def reserve(number):
            try:
                return service.reserve(f"job-{number}", f"owner-{number}", "tryon_basic")["id"]
            except ServiceError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(results.count("global_budget_exhausted"), 5)
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT SUM(estimated_cents) FROM model_usage").fetchone()[0], 60)
        other = UsageService(self.connection)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            duplicates = list(pool.map(lambda _: other.reserve("same", "one", "tryon_basic")["id"], range(6)))
        self.assertEqual(len(set(duplicates)), 1)
        self.assertEqual(other.summary("one")["committed_cents"], 20)

    def test_job_and_user_limits_apply_to_every_owner(self):
        service = UsageService(self.connection, UsagePolicy(job_limit_cents=40, user_limit_cents=60))
        service.reserve("first", "admin", "tryon_basic", "a")
        service.reserve("first", "admin", "tryon_basic", "b")
        self.assert_code("job_budget_exhausted", lambda: service.reserve("first", "admin", "tryon_basic", "c"))
        service.reserve("second", "admin", "tryon_basic")
        self.assert_code("user_budget_exhausted", lambda: service.reserve("third", "admin", "tryon_basic"))
        service.reserve("fourth", "other", "tryon_basic")
        self.assertEqual(service.summary("other")["committed_cents"], 20)

    def test_unknown_and_submitted_failures_keep_budget(self):
        reservation = self.service.reserve("one", "owner", "tryon_basic")
        record = self.service.settle(reservation["id"], "unknown", {"request_id": "remote"})
        self.assertEqual(record["amount_state"], "unknown")
        self.assert_code("usage_state_conflict", lambda: self.service.settle(record["id"], "released"))
        self.assertEqual(self.service.summary("owner")["unknown_cents"], 20)
        self.service.settle(record["id"], "succeeded", {"images": 1})
        summary = self.service.summary("owner")
        self.assertEqual(summary["estimated_cents"], 20)
        self.assertEqual(summary["unknown_cents"], 0)
        self.assertFalse(summary["actual_bill_available"])
        another = self.service.reserve("two", "owner", "tryon_plus")
        self.service.settle(another["id"], "submitted")
        self.service.settle(another["id"], "failed")
        self.assertEqual(self.service.summary("owner")["committed_cents"], 70)
        self.assertEqual(self.service.summary("owner")["unknown_cents"], 50)

    def test_unsent_cancel_releases_only_once(self):
        record = self.service.reserve("one", "owner", "tryon_basic")
        self.service.settle(record["id"], "released")
        self.service.settle(record["id"], "released")
        self.assertEqual(self.service.summary("owner")["committed_cents"], 0)
        self.assert_code("usage_state_conflict", lambda: self.service.settle(record["id"], "succeeded"))
        # The same request remains cancelled; it never becomes a fresh payable call.
        again = self.service.reserve("one", "owner", "tryon_basic")
        self.assertEqual(again["id"], record["id"])
        self.assertEqual(again["status"], "released")

    def test_same_state_can_persist_remote_result_and_usage(self):
        record = self.service.reserve("remote-result", "owner", "tryon_basic")
        self.service.settle(record["id"], "submitted")
        acknowledgement = {"result": {"task_id": "remote-1"}, "reported_usage": {"images": 1}}
        self.service.settle(record["id"], "submitted", acknowledgement)
        self.assertEqual(self.service.get(record["id"], "owner")["usage"], acknowledgement)
        self.service.settle(record["id"], "succeeded")
        proof = {"result": {"task_id": "remote-1", "url": "https://example.invalid/image"},
                 "reported_usage": {"images": 1}}
        self.service.settle(record["id"], "succeeded", proof)
        actual = self.service.get(record["id"], "owner")
        self.assertEqual(actual["usage"], proof)
        self.assertEqual(actual["amount_state"], "estimated")
        self.assertEqual(self.service.summary("owner")["committed_cents"], 20)

    def test_missing_estimates_fail_closed_and_configured_roles_are_recorded(self):
        self.assertEqual(self.service.policy.estimate("tryon_basic"), 20)
        for role in ("recognition", "segmentation", "recommendation", "agent"):
            self.assert_code("estimate_missing", lambda role=role: self.service.policy.estimate(role))
            self.assert_code("estimate_missing", lambda role=role: self.service.reserve(role, "owner", role))
        policy = UsagePolicy(estimates={"recognition": 7, "segmentation": 9,
                                       "recommendation": 11, "agent": 13},
                             models={"recommendation": "configured-rec", "agent": "configured-agent"})
        service = UsageService(self.connection, policy)
        for role in ("recognition", "segmentation", "recommendation", "agent"):
            record = service.reserve("roles", "owner", role)
            service.settle(record["id"], "succeeded", {"input_tokens": 100, "output_tokens": 10})
        self.assertEqual(service.summary("owner")["estimated_cents"], 40)
        changed = UsageService(self.connection, UsagePolicy(models={"tryon_basic": "new-model"}))
        self.assert_code("estimate_missing", lambda: changed.reserve("changed", "owner", "tryon_basic"))
        missing_model = UsagePolicy(estimates={"agent": 10})
        self.assert_code("estimate_missing", lambda: missing_model.estimate("agent"))

    def test_owner_isolation_and_host_transaction_rollback(self):
        record = self.service.reserve("owner-job", "alice", "tryon_basic")
        self.assert_code("not_found", lambda: self.service.get(record["id"], "bob"))
        self.assert_code("not_found", lambda: self.service.settle(record["id"], "succeeded", owner_id="bob"))
        self.assert_code("usage_conflict", lambda: self.service.reserve("owner-job", "bob", "tryon_basic"))
        self.assert_code("usage_conflict", lambda: self.service.reserve("owner-job", "bob", "tryon_basic", "another-step"))
        self.assertEqual(self.service.summary("bob")["committed_cents"], 0)
        with self.assertRaises(RuntimeError):
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.service.reserve("rolled-back", "alice", "tryon_basic", connection=connection)
                raise RuntimeError("other business write failed")
        self.assertEqual(self.service.summary("alice")["committed_cents"], 20)

    def test_legacy_pending_admin_and_visitor_calls_are_counted_once_even_over_budget(self):
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE tryons(id TEXT PRIMARY KEY,owner_id TEXT,quality TEXT,status TEXT);
                CREATE TABLE jobs(id TEXT PRIMARY KEY,owner_id TEXT,kind TEXT,entity_id TEXT,
                                  remote_task_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE demo_usage(owner_id TEXT PRIMARY KEY,spent_cents INTEGER);
                INSERT INTO tryons VALUES('pending-admin','admin','basic','running');
                INSERT INTO tryons VALUES('queued-admin','admin','plus','queued');
                INSERT INTO tryons VALUES('visitor-one','visitor','basic','succeeded');
                INSERT INTO tryons VALUES('visitor-two','visitor','basic','failed');
                INSERT INTO tryons VALUES('orphan-plus','other','plus','running');
                INSERT INTO jobs(id,owner_id,kind,entity_id,remote_task_id)
                    VALUES('remote-admin-job','admin','tryon','pending-admin','remote-task');
                INSERT INTO demo_usage VALUES('visitor',40);
                INSERT INTO demo_usage VALUES('aggregate-only',60);
            """)
            connection.execute("BEGIN IMMEDIATE")
            policy = UsagePolicy(job_limit_cents=1,user_limit_cents=1,global_limit_cents=1,
                                 estimates={"tryon_plus": None})
            backfill_legacy_usage(connection, policy)
            backfill_legacy_usage(connection, policy)
            records = connection.execute(
                "SELECT job_id,owner_id,estimated_cents,status,amount_state,metadata_json FROM model_usage"
            ).fetchall()
            self.assertEqual(len(records), 5)
            admin = next(row for row in records if row[1] == "admin")
            self.assertEqual(admin[:5], ("remote-admin-job", "admin", 20, "unknown", "unknown"))
            self.assertEqual(sum(row[2] for row in records if row[1] == "visitor"), 40)
            self.assertFalse(connection.execute("SELECT 1 FROM model_usage WHERE id='legacy:visitor'").fetchone())
            plus = next(row for row in records if row[1] == "other")
            self.assertEqual(plus[0], "legacytryon:orphan-plus")
            self.assertEqual(plus[2], 50)
            self.assertIn("legacy_default_estimate", plus[5])
            self.assertFalse(connection.execute("SELECT 1 FROM model_usage WHERE job_id='legacytryon:queued-admin'").fetchone())
        self.assertEqual(self.service.summary("admin")["unknown_cents"], 20)
        self.assertEqual(self.service.summary("visitor")["committed_cents"], 40)
        self.assertEqual(self.service.summary("aggregate-only")["unknown_cents"], 60)


if __name__ == "__main__":
    unittest.main()
