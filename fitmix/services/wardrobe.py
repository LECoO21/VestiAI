from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from .context import ServiceError, UserContext


class WardrobeService:
    def __init__(self, connection_factory, data_dir: Path):
        self.db = connection_factory
        self.data_dir = Path(data_dir).resolve()

    def image_path(self, stored: str) -> Path:
        if not isinstance(stored, str) or not stored:
            raise ServiceError("图片路径无效", 409, "source_missing")
        relative = Path(stored)
        path = self.data_dir / relative
        if (
            relative.is_absolute() or ".." in relative.parts
            or relative.parts[0] not in {"originals", "garment-sources", "garments", "person-images", "tryons"}
            or not path.resolve().is_relative_to(self.data_dir) or not path.is_file()
        ):
            raise ServiceError("图片不存在或路径无效", 409, "source_missing")
        return path

    def search(self, user: UserContext, *, garment_type: str | None = None, keyword: str = "") -> list[dict]:
        if garment_type is not None and garment_type not in {"upper", "lower", "dress"}:
            raise ServiceError("服装类型不正确")
        if not isinstance(keyword, str) or len(keyword) > 240:
            raise ServiceError("搜索文字不能超过 240 个字符")
        with self.db() as connection:
            rows = connection.execute(
                """SELECT g.*,
                   (SELECT COUNT(*) FROM tryons t WHERE t.owner_id=g.owner_id AND
                     (t.upper_id=g.id OR t.lower_id=g.id OR t.dress_id=g.id)) AS usage_count,
                   (SELECT MAX(t.created_at) FROM tryons t WHERE t.owner_id=g.owner_id AND
                     (t.upper_id=g.id OR t.lower_id=g.id OR t.dress_id=g.id)) AS last_used_at
                   FROM garments g WHERE g.owner_id=? AND g.draft=0
                   ORDER BY g.created_at DESC, g.id DESC""", (user.owner_id,),
            ).fetchall()
        duplicates = duplicate_map(rows)
        items = []
        for row in rows:
            if garment_type and row["type"] != garment_type:
                continue
            if keyword and keyword.casefold() not in (row["label_zh"] + " " + row["description_zh"]).casefold():
                continue
            item = garment_json(row)
            item["duplicate_of"] = duplicates.get(row["id"])
            items.append(item)
        return items

    def garment(self, user: UserContext, garment_id: str) -> dict:
        with self.db() as connection:
            row = connection.execute(
                "SELECT * FROM garments WHERE id=? AND owner_id=? AND draft=0", (garment_id, user.owner_id),
            ).fetchone()
            if not row:
                raise ServiceError("衣物不存在", 404, "not_found")
            used = connection.execute(
                """SELECT * FROM tryons WHERE owner_id=? AND (upper_id=? OR lower_id=? OR dress_id=?)
                   ORDER BY created_at DESC""", (user.owner_id, garment_id, garment_id, garment_id),
            ).fetchall()
            source = connection.execute(
                "SELECT id, style_zh, source_kind FROM looks WHERE id=? AND owner_id=?", (row["look_id"], user.owner_id),
            ).fetchone() if row["look_id"] else None
        item = garment_json(row)
        item["source_look"] = dict(source) if source else None
        item["used_tryons"] = [tryon_json(tryon) for tryon in used]
        return item

    def looks(self, user: UserContext) -> list[dict]:
        with self.db() as connection:
            rows = connection.execute(
                "SELECT * FROM looks WHERE owner_id=? AND draft=0 ORDER BY created_at DESC", (user.owner_id,),
            ).fetchall()
            return [look_json(connection, row) for row in rows]

    def look(self, user: UserContext, look_id: str, *, draft: bool = False) -> dict:
        with self.db() as connection:
            row = connection.execute(
                "SELECT * FROM looks WHERE id=? AND owner_id=? AND draft=?", (look_id, user.owner_id, int(draft)),
            ).fetchone()
            if not row:
                raise ServiceError("穿搭不存在", 404, "not_found")
            return look_json(connection, row)

    @staticmethod
    def selected_rows(connection, user: UserContext, garment_ids: list[str], *, draft_look_id: str | None = None):
        if not isinstance(garment_ids, list) or not garment_ids or len(garment_ids) > 3:
            raise ServiceError("请选择有效衣物")
        if any(not isinstance(item, str) or not item or len(item) > 128 for item in garment_ids):
            raise ServiceError("衣物 ID 无效")
        if len(set(garment_ids)) != len(garment_ids):
            raise ServiceError("同一件衣物不能重复选择")
        rows = connection.execute(
            f"SELECT * FROM garments WHERE owner_id=? AND id IN ({','.join('?' for _ in garment_ids)})",
            (user.owner_id, *garment_ids),
        ).fetchall()
        if len(rows) != len(garment_ids):
            raise ServiceError("所选单品不存在", 404, "not_found")
        if draft_look_id:
            draft = connection.execute(
                "SELECT 1 FROM looks WHERE id=? AND owner_id=? AND draft=1", (draft_look_id, user.owner_id),
            ).fetchone()
            linked = connection.execute(
                f"SELECT garment_id FROM look_garments WHERE look_id=? AND garment_id IN ({','.join('?' for _ in garment_ids)})",
                (draft_look_id, *garment_ids),
            ).fetchall()
            if not draft or len(linked) != len(rows):
                raise ServiceError("临时搭配已失效，请重新上传", 409, "draft_expired")
        elif any(row["draft"] for row in rows):
            raise ServiceError("请先确认临时衣物", 409, "draft_not_ready")
        by_id = {row["id"]: row for row in rows}
        return [by_id[item] for item in garment_ids]

    def save_outfit(self, user: UserContext, garment_ids: list[str]) -> dict:
        destination = self.data_dir / "originals" / f"{uuid.uuid4().hex}.png"
        try:
            with self.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = self.selected_rows(connection, user, garment_ids)
                types = [row["type"] for row in rows]
                if not (types == ["dress"] or len(types) == 2 and set(types) == {"upper", "lower"}):
                    raise ServiceError("请选择一件上装和一件下装，或一件套装/连衣裙")
                canvas = Image.new("RGBA", (960, 1280), "#F5ECE7")
                column_width = canvas.width // len(rows)
                for index, row in enumerate(rows):
                    with Image.open(self.image_path(row["crop_path"])) as image:
                        garment = ImageOps.exif_transpose(image).convert("RGBA")
                        garment.thumbnail((column_width - 80, 1020), Image.Resampling.LANCZOS)
                        canvas.alpha_composite(garment, (index * column_width + (column_width - garment.width) // 2, (canvas.height - garment.height) // 2))
                destination.parent.mkdir(parents=True, exist_ok=True)
                canvas.convert("RGB").save(destination, "PNG", optimize=True)
                season_sets = [set(json.loads(row["seasons"] or "[]")) for row in rows]
                seasons = set.intersection(*season_sets) or set.union(*season_sets)
                season_order = [season for season in ("spring", "summer", "autumn", "winter") if season in seasons]
                look_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO looks(id, owner_id, source_path, source_kind, title_en, style_zh, seasons, palette)
                       VALUES (?, ?, ?, 'composition', ?, '衣橱搭配', ?, '[]')""",
                    (look_id, user.owner_id, destination.relative_to(self.data_dir).as_posix(), "＋".join(row["label_zh"] for row in rows)[:100], json.dumps(season_order, ensure_ascii=False)),
                )
                for garment_id in garment_ids:
                    connection.execute("INSERT INTO look_garments(look_id, garment_id) VALUES (?, ?)", (look_id, garment_id))
                saved = connection.execute("SELECT * FROM looks WHERE id=?", (look_id,)).fetchone()
                return look_json(connection, saved)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise ServiceError("单品图片无法使用，请检查或重新添加", 409, "source_missing") from exc
            raise

    def person_images(self, user: UserContext) -> list[dict]:
        with self.db() as connection:
            rows = connection.execute("SELECT * FROM person_images WHERE owner_id=? ORDER BY created_at, name", (user.owner_id,)).fetchall()
        return [person_image_json(row) for row in rows]

    def job(self, user: UserContext, job_id: str) -> dict:
        with self.db() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, user.owner_id)).fetchone()
            if not row:
                raise ServiceError("任务不存在", 404, "not_found")
        return {
            "id": row["id"], "kind": row["kind"], "entity_id": row["entity_id"],
            "status": row["status"], "stage": row["stage"], "progress": row["progress"],
            "error": {"code": row["error_code"], "message": row["error_message"]} if row["error_message"] else None,
        }

    def owns_file(self, user: UserContext, filename: str) -> bool:
        try:
            self.image_path(filename)
        except ServiceError:
            return False
        with self.db() as connection:
            checks = (
                ("looks", ("source_path",)),
                ("garments", ("source_path", "crop_path", "mask_path")),
                ("person_images", ("path",)),
                ("tryons", ("person_path", "upper_path", "lower_path", "dress_path", "result_path")),
            )
            return any(connection.execute(
                f"SELECT 1 FROM {table} WHERE owner_id=? AND ({' OR '.join(f'{column}=?' for column in columns)}) LIMIT 1",
                (user.owner_id, *(filename for _ in columns)),
            ).fetchone() for table, columns in checks)


def file_url(path: str | None) -> str | None:
    return f"/files/{path}" if path else None

def thumbnail_url(path: str | None, size: int = 480) -> str | None:
    return f"/thumbnails/{size}/{path}" if path else None

def garment_json(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "look_id": row["look_id"],
        "source_kind": row["source_kind"],
        "draft": bool(row["draft"]) if "draft" in keys else False,
        "source_url": file_url(row["source_path"]),
        "type": row["type"],
        "label_zh": row["label_zh"],
        "description_zh": row["description_zh"],
        "crop_url": file_url(row["crop_path"]),
        "thumbnail_url": thumbnail_url(row["crop_path"]),
        "mask_url": file_url(row["mask_path"]),
        "bbox": json.loads(row["bbox"] or "null"),
        "seasons": json.loads(row["seasons"] or "[]"),
        "favorite": bool(row["favorite"]),
        "created_at": row["created_at"],
        "usage_count": row["usage_count"] if "usage_count" in keys else 0,
        "last_used_at": row["last_used_at"] if "last_used_at" in keys else None,
        "duplicate_of": row["duplicate_of"] if "duplicate_of" in keys else None,
    }

def person_image_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": file_url(row["path"]),
        "thumbnail_url": thumbnail_url(row["path"], 240),
        "created_at": row["created_at"],
    }

def look_json(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    garments = connection.execute(
        """SELECT g.* FROM garments g JOIN look_garments lg ON lg.garment_id=g.id
           WHERE lg.look_id=? AND g.owner_id=? ORDER BY g.created_at, g.type""",
        (row["id"], row["owner_id"]),
    ).fetchall()
    return {
        "id": row["id"],
        "source_url": file_url(row["source_path"]),
        "thumbnail_url": thumbnail_url(row["source_path"]),
        "source_kind": row["source_kind"],
        "source_tryon_id": row["source_tryon_id"],
        "title_en": row["title_en"],
        "style_zh": row["style_zh"],
        "seasons": json.loads(row["seasons"] or "[]"),
        "palette": json.loads(row["palette"] or "[]"),
        "favorite": bool(row["favorite"]),
        "draft": bool(row["draft"]) if "draft" in row.keys() else False,
        "garments": [garment_json(item) for item in garments],
        "created_at": row["created_at"],
    }

def tryon_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "person_url": file_url(row["person_path"]),
        "upper_id": row["upper_id"],
        "lower_id": row["lower_id"],
        "dress_id": row["dress_id"],
        "upper_url": file_url(row["upper_path"]),
        "lower_url": file_url(row["lower_path"]),
        "dress_url": file_url(row["dress_path"]),
        "upper_label": row["upper_label"],
        "lower_label": row["lower_label"],
        "dress_label": row["dress_label"],
        "quality": row["quality"],
        "status": row["status"],
        "result_url": file_url(row["result_path"]),
        "result_thumbnail_url": thumbnail_url(row["result_path"]),
        "created_at": row["created_at"],
    }

def duplicate_map(rows: list[sqlite3.Row]) -> dict[str, str]:
    # ponytail: O(n²) is enough for a local wardrobe; index perceptual hashes if collections reach thousands.
    originals: list[sqlite3.Row] = []
    duplicates: dict[str, str] = {}
    for row in sorted(rows, key=lambda item: (item["created_at"], item["id"])):
        fingerprint = row["image_hash"]
        match = next(
            (
                item for item in originals
                if item["type"] == row["type"] and fingerprint and item["image_hash"]
                and (int(fingerprint, 16) ^ int(item["image_hash"], 16)).bit_count() <= 4
            ),
            None,
        )
        if match:
            duplicates[row["id"]] = match["id"]
        else:
            originals.append(row)
    return duplicates
