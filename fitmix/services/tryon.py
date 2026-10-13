from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .context import ServiceError, UserContext
from .wardrobe import WardrobeService, tryon_json


class TryonService:
    def __init__(self, connection_factory, data_dir: Path, reserve_visitor_quota, enqueue_tryon):
        self.db = connection_factory
        self.data_dir = Path(data_dir).resolve()
        self.wardrobe = WardrobeService(connection_factory, data_dir)
        self.reserve_visitor_quota = reserve_visitor_quota
        self.enqueue_tryon = enqueue_tryon

    def _snapshot(self, stored: str, suffix: str, created: list[Path]) -> str:
        source = self.wardrobe.image_path(stored)
        destination = self.data_dir / "tryons" / f"{uuid.uuid4().hex}-{suffix}{source.suffix or '.png'}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        created.append(destination)
        shutil.copy2(source, destination)
        return destination.relative_to(self.data_dir).as_posix()

    @staticmethod
    def _check_quality(user: UserContext, quality: str) -> None:
        if quality not in {"basic", "plus"}:
            raise ServiceError("quality 只能是 basic 或 plus")
        if user.role == "visitor" and quality != "basic":
            raise ServiceError("高质量试穿仅管理员可用", 403, "admin_only")

    def create(
        self, user: UserContext, slots: dict[str, str | None], *, quality: str = "basic",
        person_image_id: str | None = None, source_tryon_id: str | None = None,
        uploaded_person_path: str | None = None, draft_look_id: str | None = None,
    ) -> dict:
        self._check_quality(user, quality)
        if set(slots) - {"upper", "lower", "dress"}:
            raise ServiceError("服装类型不正确")
        selected = {kind: garment_id for kind, garment_id in slots.items() if garment_id}
        if "dress" in selected and len(selected) > 1:
            raise ServiceError("套装/连衣裙不能与上下装同时选择")
        created: list[Path] = []
        try:
            with self.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = self.wardrobe.selected_rows(connection, user, list(selected.values()), draft_look_id=draft_look_id)
                by_id = {row["id"]: row for row in rows}
                if any(by_id[garment_id]["type"] != kind for kind, garment_id in selected.items()):
                    raise ServiceError("所选衣物不存在或类型不匹配")
                if uploaded_person_path:
                    # Only the host upload adapter can provide a stored path;
                    # future model tools receive person_image_id instead.
                    self.wardrobe.image_path(uploaded_person_path)
                    person_path = uploaded_person_path
                elif person_image_id:
                    person = connection.execute("SELECT path FROM person_images WHERE id=? AND owner_id=?", (person_image_id, user.owner_id)).fetchone()
                    if not person:
                        raise ServiceError("保存的形象照不存在，请重新选择", 404, "not_found")
                    person_path = self._snapshot(person["path"], "person", created)
                elif source_tryon_id:
                    previous = connection.execute("SELECT person_path FROM tryons WHERE id=? AND owner_id=?", (source_tryon_id, user.owner_id)).fetchone()
                    if not previous:
                        raise ServiceError("原试穿形象照不存在，请重新选择", 404, "not_found")
                    person_path = self._snapshot(previous["person_path"], "person", created)
                else:
                    raise ServiceError("请选择全身正面形象照")
                snapshots = {kind: self._snapshot(by_id[garment_id]["crop_path"], kind, created) for kind, garment_id in selected.items()}
                if user.role == "visitor" and not self.reserve_visitor_quota(connection, user.owner_id):
                    raise ServiceError("本次演示额度已用完", 429, "demo_quota_exhausted")
                tryon_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO tryons(id, owner_id, person_path, upper_id, lower_id, dress_id,
                       upper_path, lower_path, dress_path, upper_label, lower_label, dress_label,
                       draft_look_id, quality, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
                    (tryon_id, user.owner_id, person_path, *(selected.get(kind) for kind in ("upper", "lower", "dress")),
                     *(snapshots.get(kind) for kind in ("upper", "lower", "dress")),
                     *(by_id[selected[kind]]["label_zh"] if kind in selected else None for kind in ("upper", "lower", "dress")), draft_look_id, quality),
                )
                connection.execute(
                    """INSERT INTO jobs(id, owner_id, kind, entity_id, status, stage, progress)
                       VALUES (?, ?, 'tryon', ?, 'queued', 'queued', 0)""", (job_id, user.owner_id, tryon_id),
                )
        except Exception as exc:
            for path in created:
                path.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise ServiceError("人物或衣物图片无法读取", 409, "source_missing") from exc
            raise
        self._enqueue(job_id, tryon_id)
        return {"tryon_id": tryon_id, "job_id": job_id}

    def plus(self, user: UserContext, source_tryon_id: str) -> dict:
        self._check_quality(user, "plus")
        created: list[Path] = []
        try:
            with self.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                source = connection.execute("SELECT * FROM tryons WHERE id=? AND owner_id=?", (source_tryon_id, user.owner_id)).fetchone()
                if not source:
                    raise ServiceError("试穿记录不存在", 404, "not_found")
                person_path = self._snapshot(source["person_path"], "person", created)
                snapshots = {kind: self._snapshot(source[f"{kind}_path"], kind, created) if source[f"{kind}_path"] else None for kind in ("upper", "lower", "dress")}
                if not any(snapshots.values()):
                    raise ServiceError("所选衣物已经不存在", 409, "source_missing")
                tryon_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO tryons(id, owner_id, person_path, upper_id, lower_id, dress_id,
                       upper_path, lower_path, dress_path, upper_label, lower_label, dress_label,
                       draft_look_id, quality, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'plus', 'queued')""",
                    (tryon_id, user.owner_id, person_path, *(source[f"{kind}_id"] for kind in ("upper", "lower", "dress")),
                     *(snapshots[kind] for kind in ("upper", "lower", "dress")),
                     *(source[f"{kind}_label"] for kind in ("upper", "lower", "dress")), source["draft_look_id"]),
                )
                connection.execute("""INSERT INTO jobs(id, owner_id, kind, entity_id, status, stage, progress)
                                      VALUES (?, ?, 'tryon', ?, 'queued', 'queued', 0)""", (job_id, user.owner_id, tryon_id))
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        self._enqueue(job_id, tryon_id)
        return {"tryon_id": tryon_id, "job_id": job_id}

    def _enqueue(self, job_id: str, tryon_id: str) -> None:
        try:
            self.enqueue_tryon(job_id, tryon_id)
        except Exception as exc:
            with self.db() as connection:
                connection.execute("UPDATE jobs SET status='failed', stage='failed', error_code='queue_unavailable', error_message='后台任务未启动' WHERE id=?", (job_id,))
                connection.execute("UPDATE tryons SET status='failed' WHERE id=?", (tryon_id,))
            raise ServiceError("后台任务未启动，请检查服务", 503, "queue_unavailable") from exc

    def history(self, user: UserContext) -> list[dict]:
        with self.db() as connection:
            rows = connection.execute("SELECT * FROM tryons WHERE owner_id=? ORDER BY created_at DESC", (user.owner_id,)).fetchall()
        return [tryon_json(row) for row in rows]
