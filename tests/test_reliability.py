"""Paid-task acceptance tests use isolated data and simulated model responses."""
import importlib
import io
import json
import os
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from vestiai.bailian import BailianError, BailianSubmissionUnknown
from vestiai.services.context import ServiceError, UserContext
from vestiai.services.tryon import TryonService
from vestiai.services.usage import UsagePolicy, UsageService


class SimulatedTryon:
    configured = True

    def __init__(self, *, submission_unknown=False, upload_failure=False, wait_failures=()):
        self.submission_unknown = submission_unknown
        self.upload_failure = upload_failure
        self.wait_failures = list(wait_failures)
        self.submissions = []
        self.queries = []
        self.downloads = []
        self.last_usage = {"availability": "not_reported"}

    def submit_tryon(self, person, top, bottom, quality, *, on_submitting=None):
        if self.upload_failure:
            raise BailianError("upload_failed", "upload failed before paid submission")
        if on_submitting:
            on_submitting()
        self.submissions.append((person, top, bottom, quality))
        if self.submission_unknown:
            raise BailianSubmissionUnknown("response lost after acceptance")
        return "remote-original", "submit-request"

    def wait_tryon(self, task_id, timeout_seconds=180):
        self.queries.append(task_id)
        if self.wait_failures:
            raise self.wait_failures.pop(0)
        self.last_usage = {"image_count": 1}
        return "https://example.test/original-result.png"

    def download(self, url, destination):
        self.downloads.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 400), "navy").save(destination, "PNG")


class SimulatedImport:
    configured = True

    def __init__(self, *, download_failure=False, unknown_step=None):
        self.download_failure = download_failure
        self.unknown_step = unknown_step
        self.recognition_calls = 0
        self.segmentation_calls = 0
        self.download_calls = 0
        self.last_usage = {"availability": "not_reported"}

    def analyze_look(self, _path, _source_kind="wardrobe", *, on_submitting=None):
        if on_submitting:
            on_submitting()
        self.recognition_calls += 1
        if self.unknown_step == "recognition":
            raise BailianSubmissionUnknown("recognition response lost")
        self.last_usage = {"input_tokens": 100, "output_tokens": 10}
        return {"title_en": "Test", "style_zh": "日常", "seasons": ["spring"], "palette": [],
                "garments": [{"type": "upper", "label_zh": "测试上装", "description_zh": "深色衬衫"}]}

    def parse_garments(self, _path, types, *, on_submitting=None):
        if on_submitting:
            on_submitting()
        self.segmentation_calls += 1
        if self.unknown_step == "segmentation":
            raise BailianSubmissionUnknown("segmentation response lost")
        self.last_usage = {"image_count": 1}
        return [{"type": kind, "crop_url": "https://example.test/crop.png", "parsing_url": None, "bbox": [1, 2, 3, 4]} for kind in types]

    def download(self, _url, destination):
        self.download_calls += 1
        if self.download_failure and self.download_calls == 1:
            raise BailianError("network_error", "download interrupted")
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (240, 320), "navy").save(destination, "PNG")


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vestiai-reliability-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("VESTIAI_", "DASHSCOPE_")) and key != "WARDROBE_DATA_DIR"
        }
        environment.update(WARDROBE_DATA_DIR=str(root / "data"), VESTIAI_CONFIG_PATH=str(root / "no-credentials.env"))
        self.isolate(patch.dict(os.environ, environment, clear=True))
        self.wardrobe = importlib.import_module("vestiai.app")
        self.isolate(patch.object(self.wardrobe, "DATA_DIR", root / "data"))
        self.isolate(patch.object(self.wardrobe, "DB_PATH", root / "data" / "wardrobe.db"))
        self.isolate(patch.object(self.wardrobe, "CONFIG_PATH", root / "no-credentials.env"))
        self.isolate(patch.object(self.wardrobe, "AUTH_ENABLED", False))
        self.isolate(patch.object(self.wardrobe, "GLOBAL_BUDGET_CENTS", 6000))
        self.isolate(patch.object(self.wardrobe, "VISITOR_BUDGET_CENTS", 200))
        self.isolate(patch.object(self.wardrobe, "VISITOR_TRYON_LIMIT", 3))
        self.isolate(patch("requests.sessions.Session.request", side_effect=AssertionError("Real model calls are forbidden")))
        self.policy = UsagePolicy(estimates={"recognition": 1, "segmentation": 5})
        self.isolate(patch.object(self.wardrobe, "usage_policy", side_effect=lambda *_args: self.policy))
        self.isolate(patch.object(self.wardrobe, "_recovery_started", True))
        self.enqueue = self.isolate(patch.object(self.wardrobe, "enqueue"))
        for directory in ("originals", "garment-sources", "garments", "person-images", "tryons", "thumbnails"):
            (self.wardrobe.DATA_DIR / directory).mkdir(parents=True, exist_ok=True)
        self.wardrobe.init_db()
        self.isolate(patch.dict(self.wardrobe.app.config, {"TESTING": True}))
        self.client = self.wardrobe.app.test_client()
        self.user = UserContext("admin", "admin")
        self.visitor = UserContext("visitor-test", "visitor")
        self.seed("admin")
        self.seed("visitor-test")

    def isolate(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def seed(self, owner):
        garment_path = f"garments/{owner}-upper.png"
        person_path = f"person-images/{owner}.png"
        Image.new("RGB", (240, 320), "white").save(self.wardrobe.DATA_DIR / garment_path)
        Image.new("RGB", (240, 320), "gray").save(self.wardrobe.DATA_DIR / person_path)
        with self.wardrobe.db() as connection:
            connection.execute(
                """INSERT INTO garments(id,owner_id,source_kind,type,label_zh,crop_path)
                   VALUES (?,?,'direct','upper',?,?)""", (f"{owner}-upper", owner, f"{owner}衬衫", garment_path),
            )
            connection.execute("INSERT INTO person_images(id,owner_id,name,path) VALUES (?,?,?,?)",
                               (f"{owner}-person", owner, f"{owner}人物", person_path))

    def service(self, policy=None):
        return TryonService(
            self.wardrobe.db, self.wardrobe.DATA_DIR, self.wardrobe.reserve_visitor_tryon,
            lambda job_id, tryon_id: self.wardrobe.enqueue(job_id, "tryon", tryon_id, self.wardrobe.process_tryon, tryon_id),
            UsageService(self.wardrobe.db, policy or self.policy),
        )

    def authorization(self, user=None, service=None, *, confirmed=True, **options):
        user = user or self.user
        service = service or self.service()
        prepared = service.authorize(
            user, {"upper": f"{user.owner_id}-upper"}, person_image_id=f"{user.owner_id}-person", **options,
        )
        if confirmed:
            service.confirm(user, **self.ids(prepared))
        return prepared

    @staticmethod
    def ids(authorization):
        return {key: authorization[key] for key in ("authorization_id", "request_id")}

    def create_authorized(self, user=None, service=None):
        user = user or self.user
        service = service or self.service()
        authorization = self.authorization(user, service)
        return service.create(user, **self.ids(authorization))

    def import_job(self, entity="imported-upper"):
        stored_source = f"garment-sources/{entity}.png"
        Image.new("RGB", (320, 400), "purple").save(self.wardrobe.DATA_DIR / stored_source)
        job_id = self.wardrobe.create_job("garment", entity, stored_source, self.user.owner_id)
        return job_id, stored_source

    def run_import(self, job_id, source):
        entity = self.row("jobs", job_id)["entity_id"]
        self.wardrobe.run_job(job_id, "garment", entity, self.wardrobe.process_direct_garment,
                              entity, source, None, self.user.owner_id)

    def row(self, table, identity):
        with self.wardrobe.db() as connection:
            return dict(connection.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone())

    def assert_unconsumed(self, authorization, owner="admin"):
        with self.wardrobe.db() as connection:
            for table in ("jobs", "tryons", "model_usage"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (owner,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM demo_usage WHERE owner_id=?", (owner,)).fetchone()[0], 0)
        self.assertIsNone(self.row("tryon_authorizations", authorization["authorization_id"])["consumed_job_id"])
        self.assertEqual(list((self.wardrobe.DATA_DIR / "tryons").iterdir()), [])

    def test_concurrent_calls_consume_one_authorization_and_enqueue_once(self):
        service = self.service()
        authorization = self.authorization(self.visitor, service)
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait(timeout=10)
            return service.create(self.visitor, **self.ids(authorization))

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(submit) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual({result["job_id"] for result in results}, {results[0]["job_id"]})
        self.assertEqual({result["tryon_id"] for result in results}, {results[0]["tryon_id"]})
        self.assertEqual(sorted(result["reused"] for result in results), [False, True])
        self.assertEqual(self.enqueue.call_count, 1)
        with self.wardrobe.db() as connection:
            for table in ("jobs", "tryons", "model_usage"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)
            quota = connection.execute("SELECT basic_tryons,spent_cents FROM demo_usage WHERE owner_id=?", (self.visitor.owner_id,)).fetchone()
            self.assertEqual(tuple(quota), (1, 20))
        self.assertEqual(len(list((self.wardrobe.DATA_DIR / "tryons").iterdir())), 2)

    def test_expired_wrong_owner_or_mismatched_request_cannot_consume(self):
        authorization = self.authorization()
        service = self.service()
        for user, parameters, expected in (
            (self.visitor, self.ids(authorization), "authorization_not_found"),
            (self.user, {**self.ids(authorization), "request_id": "0" * 32}, "request_mismatch"),
        ):
            with self.subTest(expected=expected), self.assertRaises(ServiceError) as caught:
                service.create(user, **parameters)
            self.assertEqual(caught.exception.code, expected)
        with self.wardrobe.db() as connection:
            connection.execute("UPDATE tryon_authorizations SET expires_at=0 WHERE id=?", (authorization["authorization_id"],))
        with self.assertRaises(ServiceError) as caught:
            service.create(self.user, **self.ids(authorization))
        self.assertEqual(caught.exception.code, "authorization_expired")
        self.assert_unconsumed(authorization)

    def test_quality_or_selection_cannot_expand_authorized_scope(self):
        authorization = self.authorization()
        for options in ({"quality": "plus"}, {"person_image_id": "visitor-test-person"}, {"slots": {"upper": "visitor-test-upper"}}):
            with self.subTest(options=options), self.assertRaises(ServiceError) as caught:
                self.service().create(self.user, **self.ids(authorization), **options)
            self.assertEqual(caught.exception.code, "authorization_mismatch")
        self.assert_unconsumed(authorization)
        with self.assertRaises(ServiceError) as caught:
            self.authorization(self.visitor, quality="plus")
        self.assertEqual(caught.exception.code, "admin_only")

    def test_changed_person_or_garment_requires_new_confirmation(self):
        for relative in ("person-images/admin.png", "garments/admin-upper.png"):
            with self.subTest(relative=relative):
                authorization = self.authorization()
                path = self.wardrobe.DATA_DIR / relative
                original = path.read_bytes()
                path.write_bytes(original + b"changed after confirmation")
                with self.assertRaises(ServiceError) as caught:
                    self.service().create(self.user, **self.ids(authorization))
                self.assertEqual(caught.exception.code, "authorization_mismatch")
                self.assert_unconsumed(authorization)
                path.write_bytes(original)

    def test_budget_failure_rolls_back_records_quota_authorization_and_snapshots(self):
        for scope in ("job", "user", "global"):
            with self.subTest(scope=scope):
                service = self.service(UsagePolicy(**{f"{scope}_limit_cents": 19}))
                authorization = self.authorization(self.visitor, service)
                with self.assertRaises(ServiceError) as caught:
                    service.create(self.visitor, **self.ids(authorization))
                self.assertEqual(caught.exception.code, f"{scope}_budget_exhausted")
                self.assert_unconsumed(authorization, self.visitor.owner_id)
        self.enqueue.assert_not_called()

    def test_http_creation_requires_confirmation_and_rejects_extra_scope(self):
        legacy = self.client.post("/api/tryons", data={"upper_id": "admin-upper", "person_image_id": "admin-person"})
        self.assertGreaterEqual(legacy.status_code, 400)
        response = self.client.post("/api/tryon-authorizations", data={"upper_id": "admin-upper", "person_image_id": "admin-person", "quality": "basic"})
        self.assertIn(response.status_code, (200, 201), response.json)
        authorization = response.json
        unconfirmed = self.client.post("/api/tryons", json=self.ids(authorization))
        self.assertEqual(unconfirmed.status_code, 403, unconfirmed.json)
        self.assert_unconsumed(authorization)
        confirmed = self.client.post(f"/api/tryon-authorizations/{authorization['authorization_id']}/confirm",
                                     json={"request_id": authorization["request_id"]})
        self.assertIn(confirmed.status_code, (200, 201), confirmed.json)
        for extra in ({"quality": "plus"}, {"owner_id": "visitor-test"}, {"upper_id": "visitor-test-upper"}, {"count": 2}):
            with self.subTest(extra=extra):
                blocked = self.client.post("/api/tryons", json={**self.ids(authorization), **extra})
                self.assertGreaterEqual(blocked.status_code, 400)
        created = self.client.post("/api/tryons", json=self.ids(authorization))
        repeated = self.client.post("/api/tryons", json=self.ids(authorization))
        self.assertEqual(created.status_code, 202, created.json)
        self.assertEqual(repeated.status_code, 202, repeated.json)
        self.assertEqual(created.json["job_id"], repeated.json["job_id"])
        self.assertEqual(self.enqueue.call_count, 1)

    def test_prepared_quote_cannot_be_consumed_without_host_confirmation(self):
        authorization = self.authorization(confirmed=False)
        with self.assertRaises(ServiceError) as caught:
            self.service().create(self.user, **self.ids(authorization))
        self.assertEqual(caught.exception.status, 403)
        self.assert_unconsumed(authorization)
        self.enqueue.assert_not_called()

    def test_confirmation_is_idempotent_and_expiry_does_not_expand_consumed_scope(self):
        service = self.service()
        prepared = self.authorization(service=service, confirmed=False)
        first = service.confirm(self.user, **self.ids(prepared))
        second = service.confirm(self.user, **self.ids(prepared))
        self.assertEqual(first, second)
        self.assert_unconsumed(prepared)
        with self.wardrobe.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tryon_authorizations").fetchone()[0], 1)
        self.assertEqual(self.row("tryon_authorizations", prepared["authorization_id"])["expires_at"], prepared["expires_at"])
        created = service.create(self.user, **self.ids(prepared))
        with self.wardrobe.db() as connection:
            connection.execute("UPDATE tryon_authorizations SET expires_at=0 WHERE id=?", (prepared["authorization_id"],))
        reused = service.create(self.user, **self.ids(prepared))
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["job_id"], created["job_id"])
        self.assertEqual(self.enqueue.call_count, 1)
        self.assertEqual(service.usage.summary(self.user.owner_id)["committed_cents"], 20)

    def test_expired_unconsumed_quote_cannot_be_confirmed(self):
        prepared = self.authorization(confirmed=False)
        with self.wardrobe.db() as connection:
            connection.execute("UPDATE tryon_authorizations SET expires_at=0 WHERE id=?", (prepared["authorization_id"],))
        with self.assertRaises(ServiceError) as caught:
            self.service().confirm(self.user, **self.ids(prepared))
        self.assertEqual(caught.exception.code, "authorization_expired")
        self.assert_unconsumed(prepared)

    def test_unknown_submission_is_preserved_without_second_paid_request(self):
        created = self.create_authorized()
        client = SimulatedTryon(submission_unknown=True)
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            for _ in range(2):
                self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.queries, [])
        self.assertEqual(self.row("jobs", created["job_id"])["status"], "unknown")
        self.assertEqual(self.row("tryons", created["tryon_id"])["status"], "unknown")
        summary = self.service().usage.summary(self.user.owner_id)
        self.assertEqual(summary["unknown_cents"], 20)
        self.assertEqual(summary["committed_cents"], 20)

    def test_existing_remote_id_resumes_query_and_download_without_submission(self):
        created = self.create_authorized()
        self.wardrobe.set_job(created["job_id"], status="waiting_result", stage="generating", remote_task_id="original-saved-id")
        with self.wardrobe.db() as connection:
            connection.execute("UPDATE tryons SET status='waiting_result' WHERE id=?", (created["tryon_id"],))
        self.wardrobe.recover_jobs()
        client = SimulatedTryon()
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
        self.assertEqual(client.submissions, [])
        self.assertEqual(client.queries, ["original-saved-id"])
        self.assertEqual(len(client.downloads), 1)
        self.assertEqual(self.row("jobs", created["job_id"])["status"], "succeeded")
        result = self.row("tryons", created["tryon_id"])
        self.assertEqual(result["status"], "succeeded")
        with Image.open(self.wardrobe.DATA_DIR / result["result_path"]) as image:
            image.verify()

    def test_poll_timeout_remains_waiting_and_resumes_same_task(self):
        created = self.create_authorized()
        client = SimulatedTryon(wait_failures=[BailianError("tryon_timeout", "original task still running")])
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
            self.assertEqual(self.row("jobs", created["job_id"])["status"], "waiting_result")
            self.assertEqual(self.row("tryons", created["tryon_id"])["status"], "waiting_result")
            self.wardrobe.recover_jobs()
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.queries, ["remote-original", "remote-original"])
        self.assertEqual(self.row("jobs", created["job_id"])["status"], "succeeded")

    def test_accepted_async_submission_keeps_recorded_input_and_remote_usage(self):
        created = self.create_authorized()
        client = SimulatedTryon(wait_failures=[BailianError("tryon_timeout", "original task still running")])
        client.last_usage = {"image_count": 1}
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
        with self.wardrobe.db() as connection:
            record = connection.execute("SELECT usage_json,status FROM model_usage WHERE job_id=? AND step='tryon'", (created["job_id"],)).fetchone()
        usage = json.loads(record["usage_json"])
        self.assertEqual(record["status"], "submitted")
        self.assertEqual(usage["input_images"], 2)
        self.assertEqual(usage["reported_usage"], {"image_count": 1})
        self.assertEqual(usage["result"], ["remote-original", "submit-request"])

    def test_completed_paid_steps_are_cached_and_unknown_steps_never_repeat(self):
        created = self.create_authorized()
        client = SimulatedTryon()
        calls = []

        def recognize(_path, *, on_submitting):
            on_submitting()
            calls.append(True)
            client.last_usage = {"input_tokens": 100, "output_tokens": 10}
            return {"type": "upper"}

        for _ in range(2):
            result = self.wardrobe.paid_call(created["job_id"], "recognition", "recognition", client, recognize, Path("test.png"))
            self.assertEqual(result, {"type": "upper"})
        self.assertEqual(calls, [True])
        uncertain = SimulatedTryon(submission_unknown=True)
        with self.assertRaises(BailianSubmissionUnknown):
            self.wardrobe.paid_call(created["job_id"], "tryon_basic", "tryon", uncertain, uncertain.submit_tryon,
                                    Path("person.png"), Path("top.png"), None, "basic", async_task=True)
        with self.assertRaises(BailianSubmissionUnknown) as caught:
            self.wardrobe.paid_call(created["job_id"], "tryon_basic", "tryon", uncertain, uncertain.submit_tryon,
                                    Path("person.png"), Path("top.png"), None, "basic", async_task=True)
        self.assertIn("未重复提交", str(caught.exception))
        self.assertEqual(len(uncertain.submissions), 1)

    def test_cached_recognition_and_segmentation_resume_download_without_new_post(self):
        job_id, source = self.import_job()
        client = SimulatedImport(download_failure=True)
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            self.run_import(job_id, source)
            self.assertEqual(self.row("jobs", job_id)["status"], "failed")
            resumed = self.client.post(f"/api/jobs/{job_id}/resume")
            self.assertEqual(resumed.status_code, 202, resumed.json)
            self.run_import(job_id, source)
        self.assertEqual(self.row("jobs", job_id)["status"], "succeeded")
        self.assertEqual(client.recognition_calls, 1)
        self.assertEqual(client.segmentation_calls, 1)
        self.assertEqual(client.download_calls, 2)
        self.assertEqual(self.row("garments", "imported-upper")["owner_id"], self.user.owner_id)
        self.assertEqual(self.service().usage.summary(self.user.owner_id)["committed_cents"], 6)

    def test_import_recovery_after_garment_commit_preserves_source_and_existing_crop(self):
        job_id, source = self.import_job()
        client = SimulatedImport()
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            # The garment transaction committed, but the worker did not get to
            # mark its job complete before the process exited.
            self.wardrobe.process_direct_garment(job_id, "imported-upper", source, None, self.user.owner_id)
            original = self.row("garments", "imported-upper")
            original_crop = (self.wardrobe.DATA_DIR / original["crop_path"]).read_bytes()
            original_source = (self.wardrobe.DATA_DIR / source).read_bytes()
            self.wardrobe.set_job(job_id, status="running", stage="downloading_garments")
            self.wardrobe.recover_jobs()
            self.run_import(job_id, source)
        self.assertEqual(self.row("jobs", job_id)["status"], "succeeded")
        self.assertEqual(client.recognition_calls, 1)
        self.assertEqual(client.segmentation_calls, 1)
        self.assertEqual((self.wardrobe.DATA_DIR / source).read_bytes(), original_source)
        self.assertEqual((self.wardrobe.DATA_DIR / original["crop_path"]).read_bytes(), original_crop)
        with self.wardrobe.db() as connection:
            garments = connection.execute("SELECT * FROM garments WHERE source_path=?", (source,)).fetchall()
        self.assertEqual(len(garments), 1)
        self.assertEqual(garments[0]["id"], "imported-upper")
        self.assertEqual(garments[0]["crop_path"], original["crop_path"])
        for garment in garments:
            for key in ("source_path", "crop_path", "mask_path"):
                if garment[key]:
                    self.assertTrue((self.wardrobe.DATA_DIR / garment[key]).is_file())

    def test_sync_unknown_retains_cost_and_blocks_resume_and_recovery(self):
        for step in ("recognition", "segmentation"):
            with self.subTest(step=step):
                job_id, source = self.import_job(f"unknown-{step}")
                client = SimulatedImport(unknown_step=step)
                with patch.object(self.wardrobe, "bailian_client", return_value=client):
                    self.run_import(job_id, source)
                    self.assertEqual(self.row("jobs", job_id)["status"], "unknown")
                    blocked = self.client.post(f"/api/jobs/{job_id}/resume")
                    self.assertEqual(blocked.status_code, 409, blocked.json)
                    self.wardrobe.recover_jobs()
                    self.run_import(job_id, source)
                self.assertEqual(client.recognition_calls, 1)
                self.assertEqual(client.segmentation_calls, int(step == "segmentation"))
                self.assertEqual(client.download_calls, 0)
                with self.wardrobe.db() as connection:
                    usage = connection.execute("SELECT amount_state,estimated_cents FROM model_usage WHERE job_id=? AND step=?", (job_id, step)).fetchone()
                self.assertEqual(usage["amount_state"], "unknown")
                self.assertGreater(usage["estimated_cents"], 0)

    def test_upload_failure_before_callback_releases_unsubmitted_budget(self):
        created = self.create_authorized()
        client = SimulatedTryon(upload_failure=True)
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
        self.assertEqual(client.submissions, [])
        self.assertEqual(client.queries, [])
        self.assertEqual(self.row("jobs", created["job_id"])["status"], "failed")
        summary = self.service().usage.summary(self.user.owner_id)
        self.assertEqual(summary["released_cents"], 20)
        self.assertEqual(summary["committed_cents"], 0)

    def test_multiple_recovery_resume_and_concurrent_workers_submit_once(self):
        created = self.create_authorized(self.visitor)
        self.wardrobe.recover_jobs()
        self.wardrobe.recover_jobs()
        client = SimulatedTryon()
        self.isolate(patch.object(self.wardrobe, "AUTH_ENABLED", True))
        with self.client.session_transaction() as session:
            session["owner_id"] = self.visitor.owner_id
            session["role"] = "visitor"
        for _ in range(2):
            resumed = self.client.post(f"/api/jobs/{created['job_id']}/resume")
            self.assertEqual(resumed.status_code, 202, resumed.json)
        barrier = threading.Barrier(3)

        def run():
            barrier.wait(timeout=10)
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])

        with patch.object(self.wardrobe, "bailian_client", return_value=client), ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(run) for _ in range(3)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.queries, ["remote-original"])
        self.assertEqual(len(client.downloads), 1)
        self.assertEqual(self.row("jobs", created["job_id"])["status"], "succeeded")
        with self.wardrobe.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM model_usage WHERE job_id=?", (created["job_id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT basic_tryons FROM demo_usage WHERE owner_id=?", (self.visitor.owner_id,)).fetchone()[0], 1)

    def test_direct_concurrent_paid_calls_claim_submission_once(self):
        created = self.create_authorized()
        client = SimulatedTryon()
        both_prepared = threading.Barrier(2)
        loser_rejected = threading.Event()
        submitted = []

        def recognize(_path, *, on_submitting):
            both_prepared.wait(timeout=10)
            try:
                on_submitting()
            except ServiceError:
                loser_rejected.set()
                raise
            submitted.append(True)
            self.assertTrue(loser_rejected.wait(timeout=10))
            return {"type": "upper"}

        def call():
            try:
                return self.wardrobe.paid_call(created["job_id"], "recognition", "recognition", client, recognize, Path("test.png"))
            except BailianSubmissionUnknown:
                return "other_call_in_progress"

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(call) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(submitted, [True])
        self.assertIn({"type": "upper"}, results)
        self.assertIn("other_call_in_progress", results)
        with self.wardrobe.db() as connection:
            records = connection.execute("SELECT status,amount_state,usage_json FROM model_usage WHERE job_id=? AND step='recognition'", (created["job_id"],)).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "succeeded")
        self.assertEqual(records[0]["amount_state"], "estimated")
        self.assertEqual(json.loads(records[0]["usage_json"])["result"], {"type": "upper"})

    def test_import_budget_or_missing_estimate_leaves_no_job_look_file_or_post(self):
        image = io.BytesIO()
        Image.effect_noise((420, 520), 90).convert("RGB").save(image, "JPEG", quality=92)
        content = image.getvalue()
        original_files = {path.relative_to(self.wardrobe.DATA_DIR) for path in self.wardrobe.DATA_DIR.rglob("*") if path.is_file() and path.suffix == ".png"}
        for policy, code in (
            (UsagePolicy(job_limit_cents=5, estimates={"recognition": 1, "segmentation": 5}), "job_budget_exhausted"),
            (UsagePolicy(estimates={"recognition": None, "segmentation": 5}), "estimate_missing"),
            (UsagePolicy(estimates={"recognition": 1, "segmentation": None}), "estimate_missing"),
        ):
            self.policy = policy
            for route in ("/api/looks", "/api/tryon-drafts", "/api/garments"):
                with self.subTest(code=code, route=route):
                    response = self.client.post(route, data={"image": (io.BytesIO(content), "input.jpg")}, content_type="multipart/form-data")
                    self.assertGreaterEqual(response.status_code, 400)
                    self.assertEqual(response.json["error"]["code"], code)
                    with self.wardrobe.db() as connection:
                        for table in ("jobs", "looks", "model_usage"):
                            self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM garments").fetchone()[0], 2)
                    self.assertEqual(list((self.wardrobe.DATA_DIR / "originals").iterdir()), [])
                    self.assertEqual(list((self.wardrobe.DATA_DIR / "garment-sources").iterdir()), [])
                    self.assertEqual({path.relative_to(self.wardrobe.DATA_DIR) for path in self.wardrobe.DATA_DIR.rglob("*.png")}, original_files)
        self.enqueue.assert_not_called()

    def test_platform_confirmed_failure_is_final_and_cannot_resume_generation(self):
        created = self.create_authorized()
        client = SimulatedTryon(wait_failures=[BailianError("tryon_failed", "platform confirmed failure")])
        with patch.object(self.wardrobe, "bailian_client", return_value=client):
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
            self.assertEqual(self.row("jobs", created["job_id"])["status"], "failed")
            blocked = self.client.post(f"/api/jobs/{created['job_id']}/resume")
            self.assertEqual(blocked.status_code, 409, blocked.json)
            self.wardrobe.recover_jobs()
            self.wardrobe.run_job(created["job_id"], "tryon", created["tryon_id"], self.wardrobe.process_tryon, created["tryon_id"])
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.queries, ["remote-original"])
        self.assertEqual(client.downloads, [])
        with self.wardrobe.db() as connection:
            record = connection.execute("SELECT status,amount_state,estimated_cents FROM model_usage WHERE job_id=?", (created["job_id"],)).fetchone()
        self.assertEqual(record["status"], "failed")
        self.assertNotEqual(record["amount_state"], "released")
        self.assertEqual(record["estimated_cents"], 20)

    def test_init_preserves_state_and_recovery_never_resubmits_unknown(self):
        remote = self.create_authorized()
        uncertain = self.create_authorized()
        with self.wardrobe.db() as connection:
            connection.execute("UPDATE jobs SET status='running',stage='generating',remote_task_id='saved-remote' WHERE id=?", (remote["job_id"],))
            connection.execute("UPDATE jobs SET status='running',stage='submitting' WHERE id=?", (uncertain["job_id"],))
            connection.execute("UPDATE tryons SET status='running'")
        self.enqueue.reset_mock()
        self.wardrobe.init_db()
        self.enqueue.assert_not_called()
        self.assertEqual(self.row("jobs", remote["job_id"])["status"], "running")
        self.assertEqual(self.row("jobs", uncertain["job_id"])["status"], "running")
        self.wardrobe.recover_jobs()
        dispatched = [call.args[0] for call in self.enqueue.call_args_list]
        self.assertIn(remote["job_id"], dispatched)
        self.assertNotIn(uncertain["job_id"], dispatched)
        self.assertEqual(self.row("jobs", uncertain["job_id"])["status"], "unknown")
        self.assertEqual(self.row("tryons", uncertain["tryon_id"])["status"], "unknown")

    def test_schema_one_migration_keeps_remote_id_and_legacy_spend(self):
        remote = self.create_authorized()
        uncertain = self.create_authorized()
        with self.wardrobe.db() as connection:
            connection.execute("UPDATE jobs SET status='running',stage='generating',remote_task_id='legacy-remote' WHERE id=?", (remote["job_id"],))
            connection.execute("UPDATE jobs SET status='running',stage='submitting' WHERE id=?", (uncertain["job_id"],))
            connection.execute("UPDATE tryons SET status='running'")
            for table in ("jobs", "tryons"):
                sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                sql = re.sub(r'^CREATE TABLE ["`]?'+table+r'["`]?', f"CREATE TABLE {table}_legacy", sql, count=1)
                sql = sql.replace(", 'unknown', 'waiting_result'", "")
                connection.execute(sql)
                columns = ",".join(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
                connection.execute(f"INSERT INTO {table}_legacy({columns}) SELECT {columns} FROM {table}")
                connection.execute(f"DROP TABLE {table}")
                connection.execute(f"ALTER TABLE {table}_legacy RENAME TO {table}")
            connection.execute("DROP TABLE model_usage")
            connection.execute("DROP TABLE tryon_authorizations")
            connection.execute("INSERT INTO demo_usage(owner_id,basic_tryons,spent_cents) VALUES ('legacy-visitor',2,40)")
            connection.execute("PRAGMA user_version=1")
        self.enqueue.reset_mock()
        self.wardrobe.init_db()
        self.enqueue.assert_not_called()
        self.assertEqual(self.row("jobs", remote["job_id"])["remote_task_id"], "legacy-remote")
        self.assertEqual(self.row("jobs", remote["job_id"])["status"], "running")
        self.assertEqual(self.row("jobs", uncertain["job_id"])["status"], "running")
        self.assertEqual(self.service().usage.summary("legacy-visitor")["unknown_cents"], 40)
        self.wardrobe.init_db()
        self.assertEqual(self.service().usage.summary("legacy-visitor")["unknown_cents"], 40)
        self.wardrobe.recover_jobs()
        dispatched = [call.args[0] for call in self.enqueue.call_args_list]
        self.assertIn(remote["job_id"], dispatched)
        self.assertNotIn(uncertain["job_id"], dispatched)
        self.assertEqual(self.row("jobs", uncertain["job_id"])["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
