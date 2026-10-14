import io
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import vestiai.app as wardrobe


class FakeBailian:
    def analyze_look(self, _path, _source_kind="photo", *, on_submitting=None):
        if on_submitting:
            on_submitting()
        if _source_kind == "wardrobe":
            return {
                "title_en": "Wardrobe Import",
                "style_zh": "日常穿搭",
                "seasons": ["spring", "autumn"],
                "palette": [],
                "garments": [
                    {"type": "upper", "label_zh": "测试上衣", "description_zh": "白色短袖上衣"},
                    {"type": "lower", "label_zh": "测试下装", "description_zh": "深色短裙"},
                ],
            }
        return {
            "title_en": "Test Look",
            "style_zh": "测试风格",
            "seasons": ["spring"],
            "palette": [{"name_zh": "黑色", "hex": "#111111"}],
            "garments": [{"type": "upper", "label_zh": "测试上衣", "description_zh": "清晰可见"}],
        }

    def parse_garments(self, _path, types, *, on_submitting=None):
        if on_submitting:
            on_submitting()
        return [{"type": item, "crop_url": "https://example.test/crop.png", "parsing_url": "https://example.test/mask.png", "bbox": [1, 2, 3, 4]} for item in types]

    def download(self, _url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), "navy").save(destination, "PNG")

    def analyze_garment(self, _path):
        return {
            "type": "lower",
            "label_zh": "测试半身裙",
            "description_zh": "深色短裙",
            "seasons": ["spring", "autumn"],
        }


def sample_jpeg() -> bytes:
    image = Image.effect_noise((420, 520), 90).convert("RGB")
    output = io.BytesIO()
    image.save(output, "JPEG", quality=92)
    return output.getvalue()


class AppTests(unittest.TestCase):
    def test_demo_deployment_requires_auth_and_tmp_storage(self):
        wardrobe.validate_deployment_config("", Path("/not-tmp"), "", "", "")
        with self.assertRaises(RuntimeError):
            wardrobe.validate_deployment_config("demo", Path("/tmp/vestiai"), "", "admin", "x" * 32)
        with self.assertRaises(RuntimeError):
            wardrobe.validate_deployment_config("demo", Path("/tmp/vestiai"), "same", "same", "x" * 32)
        with self.assertRaises(RuntimeError):
            wardrobe.validate_deployment_config("demo", Path("/var/vestiai"), "visitor", "admin", "x" * 32)
        wardrobe.validate_deployment_config("demo", Path("/tmp/vestiai"), "visitor", "admin", "x" * 32)

    def setUp(self):
        self.environment = patch.dict(os.environ, {"VESTIAI_RECOVERY_DISABLED":"1", "VESTIAI_RECOGNITION_CENTS":"5", "VESTIAI_SEGMENTATION_CENTS":"10"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        wardrobe.DATA_DIR = Path(self.temp.name) / "data"
        wardrobe.DB_PATH = wardrobe.DATA_DIR / "wardrobe.db"
        for folder in ("originals", "garment-sources", "garments", "person-images", "tryons", "thumbnails"):
            (wardrobe.DATA_DIR / folder).mkdir(parents=True)
        wardrobe.init_db()
        wardrobe.app.config.update(TESTING=True)
        self.client = wardrobe.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def post_tryon(self, client=None, **kwargs):
        client = client or self.client
        authorization = client.post("/api/tryon-authorizations", **kwargs)
        if authorization.status_code != 201:
            return authorization
        confirmation = client.post(f"/api/tryon-authorizations/{authorization.json['authorization_id']}/confirm",json={"request_id":authorization.json["request_id"]})
        if confirmation.status_code != 200:
            return confirmation
        return client.post("/api/tryons",json={key:authorization.json[key] for key in ("authorization_id","request_id")})

    def create_processed_tryon_draft(self):
        with patch.object(wardrobe, "enqueue"):
            response = self.client.post(
                "/api/tryon-drafts",
                data={"image": (io.BytesIO(sample_jpeg()), "outfit.jpg")},
                content_type="multipart/form-data",
            )
        with patch.object(wardrobe, "bailian_client", return_value=FakeBailian()):
            wardrobe.run_job(
                response.json["job_id"],
                "tryon_draft",
                response.json["draft_id"],
                wardrobe.process_look,
                response.json["draft_id"],
            )
        return response.json["draft_id"]

    def test_upload_validation_and_queued_job(self):
        response = self.client.post(
            "/api/looks",
            data={"image": (io.BytesIO(b"small"), "small.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

        with patch.object(wardrobe, "enqueue"):
            response = self.client.post(
                "/api/looks",
                data={"image": (io.BytesIO(sample_jpeg()), "look.jpg")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 202)
        job = self.client.get(f"/api/jobs/{response.json['job_id']}").json
        self.assertEqual(job["status"], "queued")

    def test_desktop_settings_are_local_and_never_return_the_key(self):
        config_path = Path(self.temp.name) / "desktop" / ".env"
        self.assertEqual(self.client.get("/api/settings").status_code, 404)
        with patch.object(wardrobe, "DESKTOP_MODE", True), patch.object(wardrobe, "CONFIG_PATH", config_path):
            settings = self.client.get("/api/settings")
            self.assertEqual(settings.json, {"workspace_id": "", "api_key_configured": False})
            blocked = self.client.post(
                "/api/settings",
                json={"workspace_id": "workspace", "api_key": "secret"},
                headers={"Origin": "https://example.test"},
            )
            self.assertEqual(blocked.status_code, 403)
            config_path.parent.mkdir(parents=True)
            config_path.write_text("VESTIAI_USER_BUDGET_CENTS=300\n",encoding="utf-8")
            saved = self.client.post(
                "/api/settings",
                json={"workspace_id": "workspace-one", "api_key": "secret-one"},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("VESTIAI_USER_BUDGET_CENTS=300",config_path.read_text(encoding="utf-8"))
            settings = self.client.get("/api/settings").json
            self.assertEqual(settings, {"workspace_id": "workspace-one", "api_key_configured": True})
            self.assertNotIn("secret-one", str(settings))
            self.client.post("/api/settings", json={"workspace_id": "workspace-two", "api_key": ""})
            self.assertIn("DASHSCOPE_WORKSPACE_ID=workspace-two", config_path.read_text(encoding="utf-8"))
            self.assertIn("DASHSCOPE_API_KEY=secret-one", config_path.read_text(encoding="utf-8"))

    def test_thumbnail_endpoint_downsamples_and_caches(self):
        source = wardrobe.DATA_DIR / "originals" / "large.png"
        Image.new("RGB", (1200, 800), "navy").save(source)
        with wardrobe.db() as connection:
            connection.execute("INSERT INTO looks(id, source_path) VALUES ('thumbnail-look', 'originals/large.png')")
        first = self.client.get("/thumbnails/240/originals/large.png")
        second = self.client.get("/thumbnails/240/originals/large.png")
        self.assertEqual((first.status_code, first.mimetype, first.data), (200, "image/webp", second.data))
        with Image.open(io.BytesIO(first.data)) as preview:
            self.assertLessEqual(max(preview.size), 240)
        self.assertEqual(len(list((wardrobe.DATA_DIR / "thumbnails").iterdir())), 1)
        first.close()
        second.close()

    def test_import_composition_records_source_kind(self):
        with patch.object(wardrobe, "enqueue"):
            response = self.client.post(
                "/api/looks",
                data={"source_kind": "composition", "image": (io.BytesIO(sample_jpeg()), "outfit.jpg")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 202)
        look = self.client.get(f"/api/looks/{response.json['look_id']}").json
        self.assertEqual(look["source_kind"], "composition")

    def test_tryon_draft_stays_hidden_until_saved(self):
        draft_id = self.create_processed_tryon_draft()
        self.assertEqual(self.client.get("/api/looks").json["items"], [])
        self.assertEqual(self.client.get("/api/garments").json["items"], [])
        draft = self.client.get(f"/api/tryon-drafts/{draft_id}").json
        self.assertTrue(draft["draft"])
        self.assertTrue(draft["garments"][0]["draft"])

        saved = self.client.post(f"/api/tryon-drafts/{draft_id}/save")
        self.assertEqual(saved.status_code, 201)
        self.assertFalse(saved.json["draft"])
        self.assertEqual(len(self.client.get("/api/looks").json["items"]), 1)
        self.assertEqual(len(self.client.get("/api/garments").json["items"]), 1)

    def test_saving_tryon_from_draft_keeps_only_result_look(self):
        draft_id = self.create_processed_tryon_draft()
        garment_id = self.client.get(f"/api/tryon-drafts/{draft_id}").json["garments"][0]["id"]
        with patch.object(wardrobe, "enqueue"):
            created = self.post_tryon(
                data={
                    "upper_id": garment_id,
                    "draft_look_id": draft_id,
                    "person": (io.BytesIO(sample_jpeg()), "person.jpg"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(created.status_code, 202)
        result = wardrobe.DATA_DIR / "tryons" / "draft-result.png"
        result.write_bytes(sample_jpeg())
        with wardrobe.db() as connection:
            connection.execute(
                "UPDATE tryons SET status='succeeded', result_path='tryons/draft-result.png' WHERE id=?",
                (created.json["tryon_id"],),
            )

        saved = self.client.post(f"/api/tryons/{created.json['tryon_id']}/save-look")
        self.assertEqual(saved.status_code, 201)
        looks = self.client.get("/api/looks").json["items"]
        garments = self.client.get("/api/garments").json["items"]
        self.assertEqual([item["source_kind"] for item in looks], ["tryon"])
        self.assertEqual(len(garments), 1)
        self.assertEqual(self.client.get(f"/api/tryon-drafts/{draft_id}").status_code, 404)

    def test_mocked_analysis_parsing_workflow(self):
        look_id = uuid.uuid4().hex
        source = wardrobe.DATA_DIR / "originals" / "look.jpg"
        source.write_bytes(sample_jpeg())
        with wardrobe.db() as connection:
            connection.execute("INSERT INTO looks(id, source_path) VALUES (?, ?)", (look_id, "originals/look.jpg"))
        job_id = wardrobe.create_job("look", look_id)
        with patch.object(wardrobe, "bailian_client", return_value=FakeBailian()):
            wardrobe.run_job(job_id, "look", look_id, wardrobe.process_look, look_id)
        job = self.client.get(f"/api/jobs/{job_id}").json
        look = self.client.get(f"/api/looks/{look_id}").json
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(look["title_en"], "Test Look")
        self.assertEqual(look["garments"][0]["type"], "upper")
        self.assertEqual(look["garments"][0]["seasons"], ["spring"])
        self.assertFalse(look["garments"][0]["favorite"])

    def test_direct_garment_workflow(self):
        garment_id = uuid.uuid4().hex
        source = wardrobe.DATA_DIR / "garment-sources" / "skirt.jpg"
        source.write_bytes(sample_jpeg())
        job_id = wardrobe.create_job("garment", garment_id)
        with patch.object(wardrobe, "bailian_client", return_value=FakeBailian()):
            wardrobe.run_job(
                job_id,
                "garment",
                garment_id,
                wardrobe.process_direct_garment,
                garment_id,
                "garment-sources/skirt.jpg",
            )
        job = self.client.get(f"/api/jobs/{job_id}").json
        garments = self.client.get("/api/garments").json["items"]
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual({item["type"] for item in garments}, {"upper", "lower"})
        self.assertTrue(all(item["source_kind"] == "direct" for item in garments))
        self.assertTrue(all(item["seasons"] == ["spring", "autumn"] for item in garments))
        self.assertTrue(all(not item["favorite"] for item in garments))

    def test_short_unique_garment_names_and_look_favorite(self):
        self.assertEqual(wardrobe.garment_name_base("灰色与白色条纹衬衫，配黑色纽扣", "upper"), "灰白条纹衬衫")
        with patch.object(wardrobe, "bailian_client", return_value=FakeBailian()):
            for index in range(2):
                garment_id = uuid.uuid4().hex
                source = wardrobe.DATA_DIR / "garment-sources" / f"skirt-{index}.jpg"
                source.write_bytes(sample_jpeg())
                job_id = wardrobe.create_job("garment",garment_id,f"garment-sources/skirt-{index}.jpg")
                wardrobe.process_direct_garment(job_id, garment_id, f"garment-sources/skirt-{index}.jpg")
        garments = self.client.get("/api/garments").json["items"]
        names = [item["label_zh"] for item in garments]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(len(name) <= 6 for name in names))

        look_id = uuid.uuid4().hex
        with wardrobe.db() as connection:
            connection.execute("INSERT INTO looks(id, source_path) VALUES (?, 'originals/look.jpg')", (look_id,))
        response = self.client.patch(f"/api/looks/{look_id}", json={"favorite": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["favorite"])

    def test_failed_direct_garment_can_retry_with_manual_type(self):
        source = wardrobe.DATA_DIR / "garment-sources" / "unknown.jpg"
        source.write_bytes(sample_jpeg())
        garment_id = uuid.uuid4().hex
        failed_job = wardrobe.create_job("garment", garment_id, "garment-sources/unknown.jpg")
        wardrobe.set_job(failed_job, status="failed", stage="failed", error_message="识别失败")
        with patch.object(wardrobe, "enqueue") as enqueue:
            response = self.client.post(f"/api/jobs/{failed_job}/retry-garment", json={"type": "dress"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.args[-2], "dress")

    def test_merge_duplicate_preserves_tryon_and_links(self):
        source_id, target_id, look_id, tryon_id = [uuid.uuid4().hex for _ in range(4)]
        for name in ("source.png", "target.png"):
            Image.new("RGB", (32, 32), "black").save(wardrobe.DATA_DIR / "garments" / name)
        with wardrobe.db() as connection:
            connection.execute("INSERT INTO looks(id, source_path) VALUES (?, 'originals/look.jpg')", (look_id,))
            connection.execute(
                """INSERT INTO garments(id, source_kind, type, label_zh, crop_path, image_hash, created_at)
                   VALUES (?, 'direct', 'upper', '重复', 'garments/source.png', '0000000000000000', '2026-01-01')""",
                (source_id,),
            )
            connection.execute(
                """INSERT INTO garments(id, source_kind, type, label_zh, crop_path, image_hash, created_at)
                   VALUES (?, 'direct', 'upper', '保留', 'garments/target.png', '0000000000000000', '2025-01-01')""",
                (target_id,),
            )
            connection.execute("INSERT INTO look_garments VALUES (?, ?)", (look_id, source_id))
            connection.execute(
                """INSERT INTO tryons(id, person_path, upper_id, upper_path, quality, status)
                   VALUES (?, 'tryons/person.jpg', ?, 'tryons/snapshot.png', 'basic', 'succeeded')""",
                (tryon_id, source_id),
            )
        listed = self.client.get("/api/garments").json["items"]
        self.assertEqual(next(item for item in listed if item["id"] == source_id)["duplicate_of"], target_id)
        response = self.client.post(f"/api/garments/{source_id}/merge", json={"target_id": target_id})
        self.assertEqual(response.status_code, 200)
        with wardrobe.db() as connection:
            self.assertEqual(connection.execute("SELECT upper_id FROM tryons WHERE id=?", (tryon_id,)).fetchone()[0], target_id)
            self.assertEqual(connection.execute("SELECT garment_id FROM look_garments WHERE look_id=?", (look_id,)).fetchone()[0], target_id)

    def test_tryon_accepts_unfavorited_wardrobe_item(self):
        garment_id = uuid.uuid4().hex
        crop = wardrobe.DATA_DIR / "garments" / "top.png"
        crop.write_bytes(b"mock")
        with wardrobe.db() as connection:
            connection.execute(
                """INSERT INTO garments(id, source_kind, type, label_zh, crop_path, favorite)
                   VALUES (?, 'direct', 'upper', '上衣', 'garments/top.png', 0)""",
                (garment_id,),
            )
        with patch.object(wardrobe, "enqueue"):
            response = self.post_tryon(
                data={"upper_id": garment_id, "person": (io.BytesIO(sample_jpeg()), "person.jpg")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 202)
        with wardrobe.db() as connection:
            tryon = connection.execute("SELECT * FROM tryons WHERE id=?", (response.json["tryon_id"],)).fetchone()
        self.assertTrue(tryon["upper_path"].startswith("tryons/"))
        self.assertTrue((wardrobe.DATA_DIR / tryon["upper_path"]).exists())

    def test_person_image_library_limit_and_reuse(self):
        saved = []
        for index in range(6):
            response = self.client.post(
                "/api/person-images",
                data={"image": (io.BytesIO(sample_jpeg()), f"person-{index}.jpg")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 201)
            saved.append(response.json)
        self.assertEqual([item["name"] for item in saved], [f"形象{index}" for index in range(1, 7)])
        overflow = self.client.post(
            "/api/person-images",
            data={"image": (io.BytesIO(sample_jpeg()), "overflow.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(overflow.status_code, 409)

        garment_id = uuid.uuid4().hex
        crop = wardrobe.DATA_DIR / "garments" / "top.png"
        Image.new("RGB", (80, 100), "white").save(crop)
        with wardrobe.db() as connection:
            connection.execute(
                "INSERT INTO garments(id, source_kind, type, label_zh, crop_path) VALUES (?, 'direct', 'upper', '白上衣', 'garments/top.png')",
                (garment_id,),
            )
        with patch.object(wardrobe, "enqueue"):
            response = self.post_tryon(
                data={"upper_id": garment_id, "person_image_id": saved[0]["id"]},
            )
        self.assertEqual(response.status_code, 202)
        with wardrobe.db() as connection:
            path = connection.execute("SELECT person_path FROM tryons WHERE id=?", (response.json["tryon_id"],)).fetchone()[0]
        self.assertTrue(path.startswith("tryons/"))
        self.assertTrue((wardrobe.DATA_DIR / path).exists())

    def test_save_two_garments_as_look_without_person(self):
        garment_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
        for filename, color in (("top.png", "white"), ("bottom.png", "navy")):
            Image.new("RGBA", (180, 260), color).save(wardrobe.DATA_DIR / "garments" / filename)
        with wardrobe.db() as connection:
            connection.execute(
                "INSERT INTO garments(id, source_kind, type, label_zh, crop_path, seasons) VALUES (?, 'direct', 'upper', '白衬衫', 'garments/top.png', '[\"spring\",\"autumn\"]')",
                (garment_ids[0],),
            )
            connection.execute(
                "INSERT INTO garments(id, source_kind, type, label_zh, crop_path, seasons) VALUES (?, 'direct', 'lower', '蓝长裤', 'garments/bottom.png', '[\"autumn\"]')",
                (garment_ids[1],),
            )
        response = self.client.post("/api/looks/from-garments", json={"garment_ids": garment_ids})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["source_kind"], "composition")
        self.assertEqual(len(response.json["garments"]), 2)
        self.assertEqual(response.json["seasons"], ["autumn"])
        self.assertTrue((wardrobe.DATA_DIR / response.json["source_url"].removeprefix("/files/")).exists())

    def test_dress_is_a_complete_saveable_outfit(self):
        Image.new("RGB", (180, 260), "navy").save(wardrobe.DATA_DIR / "garments" / "dress.png")
        with wardrobe.db() as connection:
            connection.execute("INSERT INTO garments(id, type, label_zh, crop_path) VALUES ('dress', 'dress', '蓝裙', 'garments/dress.png')")
        saved = self.client.post("/api/looks/from-garments", json={"garment_ids": ["dress"]})
        self.assertEqual(saved.status_code, 201)
        self.assertEqual([item["id"] for item in saved.json["garments"]], ["dress"])

    def test_save_outfit_rejects_invalid_input_without_writes(self):
        for body in (["dress"], {"garment_ids": "dress"}, {"garment_ids": ["dress", "dress"]}):
            self.assertEqual(self.client.post("/api/looks/from-garments", json=body).status_code, 400)
        self.assertEqual(self.client.get("/api/looks").json["items"], [])
        self.assertEqual(list((wardrobe.DATA_DIR / "originals").iterdir()), [])

    def test_newer_database_is_refused_without_changes(self):
        with wardrobe.db() as connection:
            connection.execute("PRAGMA user_version=999")
            connection.execute("INSERT INTO looks(id, source_path) VALUES ('future', 'originals/future.jpg')")
        with self.assertRaises(RuntimeError):
            wardrobe.init_db()
        with wardrobe.db() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)
            self.assertEqual(connection.execute("SELECT id FROM looks").fetchone()[0], "future")
        self.assertFalse((Path(self.temp.name) / "backups").exists())

    def test_migration_failure_rolls_back_ddl_and_keeps_backup(self):
        wardrobe.DB_PATH.unlink()
        with sqlite3.connect(wardrobe.DB_PATH) as connection:
            connection.executescript("CREATE TABLE looks(id TEXT PRIMARY KEY, source_path TEXT NOT NULL, title_en TEXT, style_zh TEXT, seasons TEXT NOT NULL DEFAULT '[]', palette TEXT NOT NULL DEFAULT '[]', raw_analysis TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP); INSERT INTO looks(id, source_path) VALUES ('old', 'originals/old.jpg');")
            before = connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        original = wardrobe._execute_statements
        calls = []
        def fail_after_ddl(connection, script):
            original(connection, script)
            calls.append(script)
            if len(calls) == 2:
                raise RuntimeError("simulated migration failure")
        with patch.object(wardrobe, "_execute_statements", side_effect=fail_after_ddl), self.assertRaises(RuntimeError):
            wardrobe.init_db()
        with sqlite3.connect(wardrobe.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall(), before)
            self.assertEqual(connection.execute("SELECT id FROM looks").fetchone()[0], "old")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
        from vestiai.backup import verify_backup
        backups = list((Path(self.temp.name) / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        verify_backup(backups[0])

    def test_local_files_do_not_expose_database_logs_or_unowned_images(self):
        (wardrobe.DATA_DIR / "wardrobe.log").write_text("private")
        Image.new("RGB", (20, 20)).save(wardrobe.DATA_DIR / "originals" / "unlinked.png")
        for path in ("wardrobe.db", "wardrobe.log", "originals/unlinked.png", "../.env"):
            self.assertEqual(self.client.get(f"/files/{path}").status_code, 404)

    def test_shared_services_enforce_identity_without_http(self):
        from vestiai.services.context import UserContext, ServiceError
        owner = UserContext("visitor:a", "visitor")
        other = UserContext("visitor:b", "visitor")
        service = wardrobe.wardrobe_service()
        for filename in ("private.png", "person.png"):
            Image.new("RGB", (180, 260)).save(wardrobe.DATA_DIR / "garments" / filename)
        with wardrobe.db() as connection:
            connection.execute("INSERT INTO garments(id, owner_id, type, label_zh, crop_path) VALUES ('private', ?, 'dress', '私有裙', 'garments/private.png')", (owner.owner_id,))
            connection.execute("INSERT INTO person_images(id, owner_id, name, path) VALUES ('private-person', ?, '形象1', 'garments/person.png')", (owner.owner_id,))
            connection.execute("INSERT INTO jobs(id, owner_id, kind, entity_id, status, stage) VALUES ('private-job', ?, 'look', 'private', 'queued', 'queued')", (owner.owner_id,))
        self.assertEqual([item["id"] for item in service.search(owner)], ["private"])
        self.assertEqual(service.search(other), [])
        self.assertFalse(service.owns_file(other, "garments/private.png"))
        self.assertTrue(service.owns_file(owner, "garments/private.png"))
        for operation in (lambda: service.garment(other, "private"), lambda: service.job(other, "private-job"), lambda: service.save_outfit(other, ["private"])):
            with self.assertRaises(ServiceError) as error:
                operation()
            self.assertEqual(error.exception.status, 404)
        with self.assertRaises(ServiceError):
            UserContext("", "admin")
        with self.assertRaises(ServiceError):
            wardrobe.current_user()
        with patch.object(wardrobe, "enqueue") as queued:
            tryons = wardrobe.tryon_service()
            with self.assertRaises(ServiceError):
                tryons.authorize(other, {"dress": "private"}, person_image_id="private-person")
            with self.assertRaises(ServiceError):
                tryons.authorize(owner, {"dress": "private"}, person_image_id="private-person", quality="plus")
            with self.assertRaises(ServiceError):
                tryons.authorize(owner, {"dress": "private"}, person_image_id="someone-else")
            queued.assert_not_called()
            self.assertEqual(list((wardrobe.DATA_DIR / "tryons").iterdir()), [])

    def test_legacy_schema_migrates_without_losing_tryon(self):
        wardrobe.DB_PATH.unlink()
        connection = sqlite3.connect(wardrobe.DB_PATH)
        connection.executescript(
            """
            CREATE TABLE looks (
                id TEXT PRIMARY KEY, source_path TEXT NOT NULL, title_en TEXT, style_zh TEXT,
                seasons TEXT NOT NULL DEFAULT '[]', palette TEXT NOT NULL DEFAULT '[]',
                raw_analysis TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE garments (
                id TEXT PRIMARY KEY, look_id TEXT NOT NULL REFERENCES looks(id) ON DELETE CASCADE,
                type TEXT NOT NULL, label_zh TEXT NOT NULL, description_zh TEXT NOT NULL DEFAULT '',
                crop_path TEXT NOT NULL, mask_path TEXT, bbox TEXT,
                favorite INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE tryons (
                id TEXT PRIMARY KEY, person_path TEXT NOT NULL,
                upper_id TEXT REFERENCES garments(id) ON DELETE CASCADE, lower_id TEXT, dress_id TEXT,
                quality TEXT NOT NULL, status TEXT NOT NULL, result_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL, status TEXT NOT NULL,
                stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, error_code TEXT,
                error_message TEXT, remote_task_id TEXT, remote_request_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO looks(id, source_path, seasons) VALUES ('look', 'originals/look.jpg', '["winter"]');
            INSERT INTO garments(id, look_id, type, label_zh, crop_path) VALUES ('top', 'look', 'upper', '旧上衣', 'garments/top.png');
            INSERT INTO tryons(id, person_path, upper_id, quality, status) VALUES ('tryon', 'tryons/person.jpg', 'top', 'basic', 'succeeded');
            """
        )
        connection.commit()
        connection.close()
        wardrobe.init_db()
        with wardrobe.db() as migrated:
            garment = migrated.execute("SELECT * FROM garments WHERE id='top'").fetchone()
            self.assertEqual(garment["source_kind"], "look")
            self.assertEqual(garment["seasons"], '["winter"]')
            self.assertEqual(garment["favorite"], 0)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM tryons").fetchone()[0], 1)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM look_garments").fetchone()[0], 1)
            self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_delete_preserves_independent_garments_and_tryon_history(self):
        look_id, garment_id, tryon_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        paths = ["originals/look.jpg", "garments/crop.png", "garments/mask.png", "tryons/person.jpg", "tryons/result.png", "tryons/top-snapshot.png"]
        for stored in paths:
            path = wardrobe.DATA_DIR / stored
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        with wardrobe.db() as connection:
            connection.execute("INSERT INTO looks(id, source_path) VALUES (?, ?)", (look_id, paths[0]))
            connection.execute(
                """INSERT INTO garments(id, look_id, type, label_zh, crop_path, mask_path, bbox)
                   VALUES (?, ?, 'upper', '上衣', ?, ?, ?)""",
                (garment_id, look_id, paths[1], paths[2], json.dumps([1, 2, 3, 4])),
            )
            connection.execute("INSERT INTO look_garments(look_id, garment_id) VALUES (?, ?)", (look_id, garment_id))
            connection.execute(
                """INSERT INTO tryons(id, person_path, upper_id, upper_path, upper_label, quality, status, result_path)
                   VALUES (?, ?, ?, ?, '上衣', 'basic', 'succeeded', ?)""",
                (tryon_id, paths[3], garment_id, paths[5], paths[4]),
            )
        self.assertEqual(self.client.delete(f"/api/looks/{look_id}").status_code, 204)
        with wardrobe.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM looks").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM garments").fetchone()[0], 1)
            preserved_garment = connection.execute("SELECT look_id, source_path FROM garments").fetchone()
            self.assertIsNone(preserved_garment["look_id"])
            self.assertTrue((wardrobe.DATA_DIR / preserved_garment["source_path"]).exists())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tryons").fetchone()[0], 1)
        self.assertFalse((wardrobe.DATA_DIR / paths[0]).exists())
        self.assertTrue(all((wardrobe.DATA_DIR / stored).exists() for stored in paths[1:]))

        self.assertEqual(self.client.delete(f"/api/garments/{garment_id}").status_code, 204)
        with wardrobe.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM garments").fetchone()[0], 0)
            preserved = connection.execute("SELECT * FROM tryons WHERE id=?", (tryon_id,)).fetchone()
            self.assertIsNone(preserved["upper_id"])
            self.assertEqual(preserved["upper_path"], paths[5])
        self.assertTrue(all((wardrobe.DATA_DIR / stored).exists() for stored in paths[3:]))

    def test_save_tryon_as_look_reuses_existing_garments(self):
        garment_id, tryon_id = uuid.uuid4().hex, uuid.uuid4().hex
        result = wardrobe.DATA_DIR / "tryons" / "result.png"
        result.write_bytes(sample_jpeg())
        with wardrobe.db() as connection:
            connection.execute(
                """INSERT INTO garments(id, source_kind, type, label_zh, crop_path, seasons)
                   VALUES (?, 'direct', 'upper', '上衣', 'garments/top.png', '["spring"]')""",
                (garment_id,),
            )
            connection.execute(
                """INSERT INTO tryons(id, person_path, upper_id, upper_path, upper_label, quality, status, result_path)
                   VALUES (?, 'tryons/person.jpg', ?, 'tryons/top.png', '上衣', 'basic', 'succeeded', 'tryons/result.png')""",
                (tryon_id, garment_id),
            )
        response = self.client.post(f"/api/tryons/{tryon_id}/save-look")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["source_kind"], "tryon")
        self.assertEqual(response.json["garments"][0]["id"], garment_id)
        self.assertEqual(self.client.post(f"/api/tryons/{tryon_id}/save-look").status_code, 200)

    def test_edit_garment_metadata(self):
        garment_id = uuid.uuid4().hex
        with wardrobe.db() as connection:
            connection.execute(
                "INSERT INTO garments(id, source_kind, type, label_zh, crop_path) VALUES (?, 'direct', 'upper', '旧名称', 'garments/top.png')",
                (garment_id,),
            )
        response = self.client.patch(
            f"/api/garments/{garment_id}",
            json={"label_zh": "针织上衣", "description_zh": "短款", "type": "upper", "seasons": ["spring", "autumn"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["label_zh"], "针织上衣")
        self.assertEqual(response.json["seasons"], ["spring", "autumn"])

    def test_tryon_rejects_dress_mixed_with_separates(self):
        response = self.client.post("/api/tryon-authorizations", data={"dress_id": "d", "upper_id": "u", "person_image_id":"person"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能", response.json["error"]["message"])

    def test_demo_password_isolates_browser_data_and_keeps_legacy_data_admin_only(self):
        image = wardrobe.DATA_DIR / "originals" / "admin.jpg"
        image.write_bytes(sample_jpeg())
        with wardrobe.db() as connection:
            connection.execute(
                "INSERT INTO looks(id, owner_id, source_path) VALUES ('admin-look', 'admin', 'originals/admin.jpg')"
            )
        with (
            patch.object(wardrobe, "AUTH_ENABLED", True),
            patch.object(wardrobe, "ACCESS_PASSWORD", "visitor-pass"),
            patch.object(wardrobe, "ADMIN_PASSWORD", "admin-pass"),
            patch.object(wardrobe, "SESSION_SECRET", "test-secret"),
        ):
            wardrobe.app.config["SESSION_COOKIE_SECURE"] = False
            visitor_a = wardrobe.app.test_client()
            visitor_b = wardrobe.app.test_client()
            admin = wardrobe.app.test_client()
            self.assertEqual(visitor_a.get("/api/looks").status_code, 401)
            self.assertEqual(visitor_a.post("/api/auth/login", json={"password": "visitor-pass"}).status_code, 200)
            self.assertEqual(visitor_a.get("/api/looks").json["items"], [])
            with patch.object(wardrobe, "enqueue"):
                created = visitor_a.post(
                    "/api/garments",
                    data={"image": (io.BytesIO(sample_jpeg()), "outfit.jpg")},
                    content_type="multipart/form-data",
                )
            self.assertEqual(created.status_code, 202)
            self.assertEqual(visitor_b.post("/api/auth/login", json={"password": "visitor-pass"}).status_code, 200)
            self.assertEqual(visitor_b.get(f"/api/jobs/{created.json['job_id']}").status_code, 404)
            self.assertEqual(visitor_b.get("/files/originals/admin.jpg").status_code, 404)
            self.assertEqual(admin.post("/api/auth/login", json={"password": "admin-pass"}).status_code, 200)
            self.assertEqual(admin.get("/api/looks").json["items"][0]["id"], "admin-look")
            admin_file = admin.get("/files/originals/admin.jpg")
            self.assertEqual(admin_file.status_code, 200)
            admin_file.close()
        wardrobe.app.config["SESSION_COOKIE_SECURE"] = wardrobe.AUTH_ENABLED

    def test_visitor_quota_is_hard_limited_and_plus_is_admin_only(self):
        with (
            patch.object(wardrobe, "AUTH_ENABLED", True),
            patch.object(wardrobe, "ACCESS_PASSWORD", "visitor-pass"),
            patch.object(wardrobe, "ADMIN_PASSWORD", "admin-pass"),
            patch.object(wardrobe, "SESSION_SECRET", "test-secret"),
        ):
            wardrobe.app.config["SESSION_COOKIE_SECURE"] = False
            client = wardrobe.app.test_client()
            client.post("/api/auth/login", json={"password": "visitor-pass"})
            with client.session_transaction() as browser_session:
                owner_id = browser_session["owner_id"]
            crop = wardrobe.DATA_DIR / "garments" / "visitor-top.png"
            person = wardrobe.DATA_DIR / "person-images" / "visitor-person.jpg"
            Image.new("RGB", (80, 100), "white").save(crop)
            person.write_bytes(sample_jpeg())
            with wardrobe.db() as connection:
                connection.execute(
                    """INSERT INTO garments(id, owner_id, source_kind, type, label_zh, crop_path)
                       VALUES ('visitor-top', ?, 'direct', 'upper', '白上衣', 'garments/visitor-top.png')""",
                    (owner_id,),
                )
                connection.execute(
                    """INSERT INTO person_images(id, owner_id, name, path)
                       VALUES ('visitor-person', ?, '形象1', 'person-images/visitor-person.jpg')""",
                    (owner_id,),
                )
            created_ids = []
            with patch.object(wardrobe, "enqueue"):
                for _ in range(3):
                    response = self.post_tryon(client=client,
                        data={"upper_id": "visitor-top", "person_image_id": "visitor-person"},
                    )
                    self.assertEqual(response.status_code, 202)
                    created_ids.append(response.json["tryon_id"])
                blocked = self.post_tryon(client=client,
                    data={"upper_id": "visitor-top", "person_image_id": "visitor-person"},
                )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.json["error"]["code"], "demo_quota_exhausted")
            self.assertEqual(client.get("/api/auth/status").json["quota"]["remaining_tryons"], 0)
            self.assertEqual(client.post(f"/api/tryons/{created_ids[0]}/retry").status_code, 403)
        wardrobe.app.config["SESSION_COOKIE_SECURE"] = wardrobe.AUTH_ENABLED


if __name__ == "__main__":
    unittest.main()
