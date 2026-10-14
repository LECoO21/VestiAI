import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

import requests

from fitmix.bailian import BailianClient, BailianError, BailianSubmissionUnknown, validate_analysis, validate_garment


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = ""
        self.content = b"image"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class BailianTests(unittest.TestCase):
    def test_analysis_schema_normalization(self):
        result = validate_analysis(
            {
                "title_en": "Soft Grunge",
                "style_zh": "柔和甜酷",
                "seasons": ["spring", "invalid"],
                "palette": [{"name_zh": "灰黑", "hex": "#333333"}, {"name_zh": "坏颜色", "hex": "red"}],
                "garments": [
                    {"type": "upper", "label_zh": "短款上衣", "description_zh": "短版剪裁"},
                    {"type": "upper", "label_zh": "重复上衣", "description_zh": "应被去重"},
                ],
            }
        )
        self.assertEqual(result["seasons"], ["spring"])
        self.assertEqual(result["palette"], [{"name_zh": "灰黑", "hex": "#333333"}])
        self.assertEqual([item["type"] for item in result["garments"]], ["upper"])

    def test_analysis_rejects_missing_garments(self):
        with self.assertRaises(BailianError):
            validate_analysis({"title_en": "x", "style_zh": "x", "seasons": [], "palette": [], "garments": []})

    def test_garment_schema_normalization(self):
        result = validate_garment(
            {"type": "lower", "label_zh": "半身裙", "description_zh": "深色短裙", "seasons": ["autumn", "bad"]}
        )
        self.assertEqual(result["type"], "lower")
        self.assertEqual(result["seasons"], ["autumn"])


    def test_parsing_response_and_task_transition(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "output": {
                            "parsing_img_url": ["https://example.test/u-mask.png", "https://example.test/l-mask.png"],
                            "crop_img_url": ["https://example.test/u.png", "https://example.test/l.png"],
                            "bbox": [[1, 2, 3, 4], [5, 6, 7, 8]],
                        }
                    }
                ),
                FakeResponse({"output": {"task_status": "RUNNING"}}),
                FakeResponse({"output": {"task_status": "SUCCEEDED", "results": [{"url": "https://example.test/result.png"}]}}),
            ]
        )
        client = BailianClient("key", "workspace", session)
        client.upload_temp = lambda *_args: "oss://temporary/input.jpg"
        parsed = client.parse_garments(Path("input.jpg"), ["upper", "lower"])
        self.assertEqual([item["type"] for item in parsed], ["upper", "lower"])
        self.assertEqual(parsed[1]["bbox"], [5, 6, 7, 8])
        with patch("fitmix.bailian.time.sleep", return_value=None):
            self.assertEqual(client.wait_tryon("task-1"), "https://example.test/result.png")

    def test_paid_tryon_network_failure_is_not_resubmitted(self):
        for quality in ("standard", "plus"):
            with self.subTest(quality=quality):
                session = FakeSession([requests.ReadTimeout("response lost"), FakeResponse({"output": {"task_id": "duplicate"}})])
                client = BailianClient("key", "workspace", session)
                client.upload_temp = lambda *_args: "oss://input.jpg"
                with self.assertRaises(BailianSubmissionUnknown) as caught:
                    client.submit_tryon(Path("person.jpg"), Path("top.jpg"), None, quality)
                self.assertEqual(caught.exception.code, "submission_unknown")
                self.assertEqual(len(session.calls), 1)
                self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_paid_sync_requests_do_not_retry_network_or_unreadable_response(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.jpg"
            image.write_bytes(b"test image")
            for operation in ("analysis", "parsing"):
                for response in (requests.ConnectionError("response lost"), FakeResponse(ValueError("invalid JSON")), FakeResponse([])):
                    with self.subTest(operation=operation, response=type(response).__name__):
                        session = FakeSession([response])
                        client = BailianClient("key", "workspace", session)
                        client.upload_temp = lambda *_args: "oss://input.jpg"
                        with self.assertRaises(BailianSubmissionUnknown):
                            if operation == "analysis":
                                client.analyze_garment(image)
                            else:
                                client.parse_garments(image, ["upper"])
                        self.assertEqual(len(session.calls), 1)

    def test_submit_missing_task_id_is_unknown_not_confirmed_failure(self):
        for payload in ({}, {"output": []}, {"output": {"task_id": []}}, {"output": {"task_id": " "}}, ValueError("invalid JSON")):
            with self.subTest(payload=payload):
                response = FakeResponse(payload)
                response.headers["x-request-id"] = "request-123"
                session = FakeSession([response])
                client = BailianClient("key", "workspace", session)
                client.upload_temp = lambda *_args: "oss://input.jpg"
                with self.assertRaises(BailianSubmissionUnknown) as caught:
                    client.submit_tryon(Path("person.jpg"), None, None, "standard")
                self.assertEqual(caught.exception.request_id, "request-123")
                self.assertEqual(len(session.calls), 1)

    def test_post_server_error_is_unknown_and_never_retried(self):
        for status in (500, 503, 307):
            with self.subTest(status=status):
                session = FakeSession([FakeResponse({"message": "error"}, status), FakeResponse({})])
                client = BailianClient("key", "workspace", session)
                with self.assertRaises(BailianSubmissionUnknown) as caught:
                    client._request("POST", "https://example.test/paid", retries=5)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(session.calls), 1)

    def test_post_explicit_rejection_keeps_failure_type(self):
        for status, code in ((400, "remote_error"), (401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited")):
            with self.subTest(status=status):
                session = FakeSession([FakeResponse({"message": "rejected"}, status)])
                client = BailianClient("key", "workspace", session)
                with self.assertRaises(BailianError) as caught:
                    client._request("POST", "https://example.test/paid")
                self.assertNotIsInstance(caught.exception, BailianSubmissionUnknown)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(session.calls), 1)

    def test_query_retries_reads_and_preserves_existing_task_id(self):
        session = FakeSession([
            requests.ReadTimeout("temporary failure"),
            FakeResponse({}, 503),
            FakeResponse({"output": {"task_status": "SUCCEEDED", "image_url": "https://example.test/result.png"}, "request_id": "query-123"}),
        ])
        client = BailianClient("key", "workspace", session)
        with patch("fitmix.bailian.time.sleep", return_value=None):
            task = client.query_tryon("original-task")
        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertEqual(task["task_id"], "original-task")
        self.assertEqual(task["request_id"], "query-123")
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(all(args[0] == "GET" and args[1].endswith("/original-task") for args, _kwargs in session.calls))

    def test_query_network_failure_has_finite_retries_and_does_not_imply_failure(self):
        session = FakeSession([requests.ConnectionError("offline") for _ in range(3)])
        client = BailianClient("key", "workspace", session)
        with patch("fitmix.bailian.time.sleep", return_value=None), self.assertRaises(BailianError) as caught:
            client.query_tryon("original-task")
        self.assertEqual(caught.exception.code, "network_error")
        self.assertEqual(len(session.calls), 3)

    def test_unknown_task_is_separate_from_confirmed_failure(self):
        for status, code in (("FAILED", "tryon_failed"), ("CANCELED", "tryon_failed"), ("UNKNOWN", "task_unknown")):
            with self.subTest(status=status):
                session = FakeSession([FakeResponse({"output": {"task_status": status}})])
                client = BailianClient("key", "workspace", session)
                with self.assertRaises(BailianError) as caught:
                    client.wait_tryon("original-task")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(session.calls[0][0][0], "GET")

    def test_wait_timeout_keeps_original_task_recoverable(self):
        session = FakeSession([FakeResponse({"output": {"task_status": "RUNNING"}})])
        client = BailianClient("key", "workspace", session)
        with patch("fitmix.bailian.time.monotonic", side_effect=[0, 0, 31]), patch("fitmix.bailian.time.sleep", return_value=None):
            with self.assertRaises(BailianError) as caught:
                client.wait_tryon("original-task", timeout_seconds=30)
        self.assertEqual(caught.exception.code, "tryon_timeout")
        self.assertIn("30", str(caught.exception))
        self.assertIn("继续查询", str(caught.exception))
        self.assertEqual(len(session.calls), 1)

    def test_submission_callback_runs_after_uploads_and_before_paid_request(self):
        session = FakeSession([FakeResponse({"output": {"task_id": "new-task"}})])
        client = BailianClient("key", "workspace", session)
        events = []
        client.upload_temp = lambda path, _model: events.append(path.name) or "oss://input.jpg"

        def reserve():
            self.assertEqual(len(session.calls), 0)
            events.append("submitting")

        task_id, _request_id = client.submit_tryon(Path("person.jpg"), Path("top.jpg"), Path("bottom.jpg"), "standard", on_submitting=reserve)
        self.assertEqual(events, ["person.jpg", "top.jpg", "bottom.jpg", "submitting"])
        self.assertEqual(task_id, "new-task")

    def test_upload_or_callback_failure_prevents_paid_submission(self):
        session = FakeSession([])
        client = BailianClient("key", "workspace", session)
        callback_calls = []

        def failed_upload(*_args):
            raise BailianError("upload_failed", "upload failed")

        client.upload_temp = failed_upload
        with self.assertRaises(BailianError):
            client.submit_tryon(Path("person.jpg"), None, None, "standard", on_submitting=lambda: callback_calls.append(True))
        self.assertEqual(callback_calls, [])

        client.upload_temp = lambda *_args: "oss://input.jpg"

        def exhausted_budget():
            raise BailianError("budget_exceeded", "budget exceeded")

        with self.assertRaises(BailianError) as caught:
            client.submit_tryon(Path("person.jpg"), None, None, "standard", on_submitting=exhausted_budget)
        self.assertEqual(caught.exception.code, "budget_exceeded")
        self.assertEqual(session.calls, [])

    def test_raw_usage_is_available_without_changing_model_results(self):
        usage = {"input_tokens": 123, "output_tokens": 20, "image_count": 1}
        garment = {"type": "upper", "label_zh": "衬衫", "description_zh": "白色", "seasons": ["spring"]}
        session = FakeSession([
            FakeResponse({"choices": [{"message": {"content": json.dumps(garment)}}], "usage": usage}),
            FakeResponse({"output": {"crop_img_url": ["https://example.test/crop.png"]}, "usage": {"image_count": 1}}),
            FakeResponse({"output": {"task_id": "task-usage"}, "usage": {"image_count": 1}}),
            FakeResponse({"output": {"task_status": "SUCCEEDED", "image_url": "https://example.test/result.png"}, "usage": {"image_count": 1, "output_images": 1}}),
        ])
        client = BailianClient("key", "workspace", session)
        client.upload_temp = lambda *_args: "oss://input.jpg"
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "input.jpg"
            image.write_bytes(b"test image")
            self.assertEqual(client.analyze_garment(image), garment)
            self.assertEqual(client.last_usage, usage)
            self.assertEqual(client.parse_garments(image, ["upper"])[0]["type"], "upper")
            self.assertEqual(client.last_usage, {"image_count": 1})
            self.assertEqual(client.submit_tryon(image, image, None, "standard"), ("task-usage", None))
            self.assertEqual(client.last_usage, {"image_count": 1})
            self.assertEqual(client.wait_tryon("task-usage"), "https://example.test/result.png")
            self.assertEqual(client.last_usage, {"image_count": 1, "output_images": 1})

    def test_missing_usage_and_network_errors_do_not_reuse_previous_counts(self):
        session = FakeSession([
            FakeResponse({"output": {"task_id": "task-1"}, "usage": {"image_count": 1}}),
            FakeResponse({"output": {"task_status": "RUNNING"}}),
            FakeResponse({"output": {"task_status": "RUNNING"}, "usage": {"image_count": 0}}),
            requests.ReadTimeout("response lost"),
        ])
        client = BailianClient("key", "workspace", session)
        client.upload_temp = lambda *_args: "oss://input.jpg"
        client.submit_tryon(Path("person.jpg"), None, None, "standard")
        self.assertEqual(client.last_usage, {"image_count": 1})
        client.query_tryon("task-1")
        self.assertEqual(client.last_usage, {"availability": "not_reported"})
        client.query_tryon("task-1")
        self.assertEqual(client.last_usage, {"image_count": 0})
        with self.assertRaises(BailianSubmissionUnknown):
            client.submit_tryon(Path("person.jpg"), None, None, "standard")
        self.assertEqual(client.last_usage, {"availability": "not_reported"})

    def test_unknown_status_with_usage_only_queries_existing_task(self):
        session = FakeSession([
            FakeResponse({}, 429),
            FakeResponse({"output": {"task_status": "UNKNOWN"}, "usage": {"image_count": 1}}),
        ])
        client = BailianClient("key", "workspace", session)
        with patch("fitmix.bailian.time.sleep", return_value=None), self.assertRaises(BailianError) as caught:
            client.wait_tryon("original-task")
        self.assertEqual(caught.exception.code, "task_unknown")
        self.assertEqual(client.last_usage, {"image_count": 1})
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(args[0] == "GET" and args[1].endswith("/original-task") for args, _kwargs in session.calls))


if __name__ == "__main__":
    unittest.main()
