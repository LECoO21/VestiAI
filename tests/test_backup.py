import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fitmix.backup import create_backup, restore_backup, verify_backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.connection = sqlite3.connect(self.data / "wardrobe.db")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE garments(id TEXT)")
        self.connection.execute("INSERT INTO garments VALUES ('preserved')")
        self.connection.commit()
        (self.data / "garments").mkdir()
        (self.data / "garments" / "coat.png").write_bytes(b"original clothes image")
        (self.data / ".env").write_text("DASHSCOPE_API_KEY=must-not-be-copied")
        (self.data / "wardrobe.log").write_text("private runtime log")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_wal_and_images_round_trip_without_credentials(self):
        backup = create_backup(self.data)
        manifest = verify_backup(backup)
        self.assertEqual(set(manifest["files"]), {"wardrobe.db", "garments/coat.png"})
        restored = restore_backup(backup, self.root / "restored")
        with sqlite3.connect(restored / "wardrobe.db") as connection:
            self.assertEqual(connection.execute("SELECT id FROM garments").fetchone()[0], "preserved")
        self.assertEqual((restored / "garments" / "coat.png").read_bytes(), b"original clothes image")
        self.assertTrue((self.data / "wardrobe.db").exists())
        self.assertTrue((restored / "thumbnails").is_dir())

    def test_changed_backup_cannot_restore(self):
        backup = create_backup(self.data)
        (backup / "garments" / "coat.png").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            restore_backup(backup, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_restore_never_overwrites_existing_data(self):
        backup = create_backup(self.data)
        with self.assertRaises(ValueError):
            restore_backup(backup, self.data)
        self.assertEqual((self.data / "garments" / "coat.png").read_bytes(), b"original clothes image")

    def test_backup_rejects_recursive_destination_and_symlinks(self):
        with self.assertRaises(ValueError):
            create_backup(self.data, self.data / "backups")
        (self.data / "garments" / "linked.png").symlink_to(self.data / ".env")
        with self.assertRaises(ValueError):
            create_backup(self.data)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_manifest_cannot_restore_paths_outside_destination(self):
        backup = create_backup(self.data)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["../outside"] = {"size": 0, "sha256": "bad"}
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            restore_backup(backup, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())


if __name__ == "__main__":
    unittest.main()
