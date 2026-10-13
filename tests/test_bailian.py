import unittest
from pathlib import Path
from unittest.mock import patch

from fitmix.bailian import BailianClient, BailianError, validate_analysis, validate_garment


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = ""
        self.content = b"image"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def request(self, *_args, **_kwargs):
        return next(self.responses)


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


if __name__ == "__main__":
    unittest.main()
