from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from .context import ServiceError, UserContext
from .wardrobe import WardrobeService, tryon_json


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tryon_authorizations (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    max_cents INTEGER NOT NULL CHECK(max_cents > 0),
    expires_at INTEGER NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
    consumed_job_id TEXT,
    consumed_tryon_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id, request_id)
);
"""


class TryonService:
    def __init__(self, connection_factory, data_dir: Path, reserve_visitor_quota, enqueue_tryon, usage_service):
        self.db = connection_factory
        self.data_dir = Path(data_dir).resolve()
        self.wardrobe = WardrobeService(connection_factory, data_dir)
        self.reserve_visitor_quota = reserve_visitor_quota
        self.enqueue_tryon = enqueue_tryon
        self.usage = usage_service

    def _snapshot(self, stored: str, suffix: str, created: list[Path]) -> str:
        source = self.wardrobe.image_path(stored)
        destination = self.data_dir / "tryons" / f"{uuid.uuid4().hex}-{suffix}{source.suffix or '.png'}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        created.append(destination)
        shutil.copy2(source, destination)
        return destination.relative_to(self.data_dir).as_posix()

    def _hash(self, stored: str) -> str:
        return hashlib.sha256(self.wardrobe.image_path(stored).read_bytes()).hexdigest()

    @staticmethod
    def _check_quality(user: UserContext, quality: str) -> None:
        if quality not in {"basic", "plus"}:
            raise ServiceError("quality 只能是 basic 或 plus")
        if user.role == "visitor" and quality != "basic":
            raise ServiceError("高质量试穿仅管理员可用", 403, "admin_only")

    def authorize(
        self, user: UserContext, slots: dict[str, str | None], *, quality="basic",
        person_image_id=None, source_tryon_id=None, uploaded_person_path=None,
        draft_look_id=None, action="new",
    ) -> dict:
        """Host-only confirmation preparation. Never expose this as a model tool."""
        self._check_quality(user, quality)
        if action not in {"new", "plus"} or action == "plus" and quality != "plus":
            raise ServiceError("生成方式不正确")
        if set(slots) - {"upper", "lower", "dress"}:
            raise ServiceError("服装类型不正确")
        selected = {kind: garment_id for kind, garment_id in slots.items() if garment_id}
        if "dress" in selected and len(selected) > 1:
            raise ServiceError("套装/连衣裙不能与上下装同时选择")
        with self.db() as connection:
            if action == "plus":
                source = connection.execute("SELECT * FROM tryons WHERE id=? AND owner_id=? AND status='succeeded'", (source_tryon_id, user.owner_id)).fetchone()
                if not source:
                    raise ServiceError("只有已完成的试穿可以升级质量", 409, "not_ready")
                selected = {kind: source[f"{kind}_id"] for kind in ("upper", "lower", "dress") if source[f"{kind}_path"]}
                if not all(selected.values()):
                    raise ServiceError("原搭配的衣服已删除，请重新选择", 409, "source_missing")
                person_path = source["person_path"]
                draft_look_id = source["draft_look_id"]
            else:
                sources = sum(bool(value) for value in (uploaded_person_path, person_image_id, source_tryon_id))
                if sources != 1:
                    raise ServiceError("请选择一张全身正面形象照")
                if uploaded_person_path:
                    person_path = uploaded_person_path
                elif person_image_id:
                    person = connection.execute("SELECT path FROM person_images WHERE id=? AND owner_id=?", (person_image_id, user.owner_id)).fetchone()
                    if not person:
                        raise ServiceError("保存的形象照不存在，请重新选择", 404, "not_found")
                    person_path = person["path"]
                else:
                    previous = connection.execute("SELECT person_path FROM tryons WHERE id=? AND owner_id=?", (source_tryon_id, user.owner_id)).fetchone()
                    if not previous:
                        raise ServiceError("原试穿形象照不存在", 404, "not_found")
                    person_path = previous["person_path"]
            if "dress" in selected and len(selected) > 1:
                raise ServiceError("套装/连衣裙不能与上下装同时选择")
            rows = self.wardrobe.selected_rows(connection, user, list(selected.values()), draft_look_id=draft_look_id)
            by_id = {row["id"]: row for row in rows}
            if any(by_id[garment_id]["type"] != kind for kind, garment_id in selected.items()):
                raise ServiceError("所选衣物类型不匹配")
            paths = {kind: (source[f"{kind}_path"] if action == "plus" else by_id[garment_id]["crop_path"]) for kind, garment_id in selected.items()}
            operation = f"tryon_{quality}"
            estimate = self.usage.policy.estimate(operation)
            spec = {
                "slots": selected, "paths": paths,
                "labels": {kind: by_id[item]["label_zh"] for kind, item in selected.items()},
                "quality": quality, "person_path": person_path,
                "person_image_id": person_image_id, "source_tryon_id": source_tryon_id,
                "draft_look_id": draft_look_id, "action": action,
                "hashes": {kind: self._hash(path) for kind, path in paths.items()},
                "current_hashes": {kind: self._hash(by_id[item]["crop_path"]) for kind, item in selected.items()},
                "person_hash": self._hash(person_path),
            }
            authorization_id, request_id = uuid.uuid4().hex, uuid.uuid4().hex
            expires_at = int(time.time()) + 900
            connection.execute("INSERT INTO tryon_authorizations(id,owner_id,request_id,spec_json,max_cents,expires_at) VALUES (?,?,?,?,?,?)",
                               (authorization_id, user.owner_id, request_id, json.dumps(spec), estimate, expires_at))
        return {"authorization_id": authorization_id, "request_id": request_id, "estimated_cents": estimate,
                "max_cents": estimate, "expires_at": expires_at, "count": 1, "quality": quality}

    def confirm(self, user: UserContext, authorization_id: str, request_id: str) -> dict:
        """Only the user's confirmation adapter calls this; never a model tool."""
        if not isinstance(authorization_id,str) or not isinstance(request_id,str):
            raise ServiceError("生成确认标识无效")
        with self.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            authorization = connection.execute("SELECT * FROM tryon_authorizations WHERE id=? AND owner_id=?", (authorization_id,user.owner_id)).fetchone()
            if not authorization:
                raise ServiceError("生成确认不存在",404,"authorization_not_found")
            if request_id != authorization["request_id"]:
                raise ServiceError("请求与生成确认不一致",409,"request_mismatch")
            if not authorization["consumed_job_id"] and authorization["expires_at"] <= time.time():
                raise ServiceError("本次生成确认已过期",409,"authorization_expired")
            self._check_quality(user,json.loads(authorization["spec_json"])["quality"])
            connection.execute("UPDATE tryon_authorizations SET confirmed=1 WHERE id=?",(authorization_id,))
        return {"authorization_id":authorization_id,"request_id":request_id,"confirmed":True}

    def create(self, user: UserContext, slots=None, *, authorization_id=None, request_id=None, **options) -> dict:
        # Optional selections are checked, never used to expand the authorization.
        if any(not isinstance(value,str) or len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value) for value in (authorization_id,request_id)):
            raise ServiceError("请先确认本次试穿和费用", 403, "authorization_required")
        created: list[Path] = []
        reused = False
        try:
            with self.db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                authorization = connection.execute("SELECT * FROM tryon_authorizations WHERE id=? AND owner_id=?", (authorization_id, user.owner_id)).fetchone()
                if not authorization:
                    raise ServiceError("本次生成确认不存在", 404, "authorization_not_found")
                if request_id != authorization["request_id"]:
                    raise ServiceError("请求与生成确认不一致", 409, "request_mismatch")
                if not authorization["confirmed"]:
                    raise ServiceError("请先确认本次试穿和费用",403,"authorization_required")
                spec = json.loads(authorization["spec_json"])
                self._check_quality(user, spec["quality"])
                if slots is not None and {key: value for key, value in slots.items() if value} != spec["slots"]:
                    raise ServiceError("搭配已改变，请重新确认", 409, "authorization_mismatch")
                if any(value != spec.get(key) for key, value in options.items()):
                    raise ServiceError("形象照或质量已改变，请重新确认", 409, "authorization_mismatch")
                if authorization["consumed_job_id"]:
                    result = {"tryon_id": authorization["consumed_tryon_id"], "job_id": authorization["consumed_job_id"], "reused": True}
                    reused = True
                else:
                    if authorization["expires_at"] <= time.time():
                        raise ServiceError("本次生成确认已过期，请重新确认", 409, "authorization_expired")
                    rows = self.wardrobe.selected_rows(connection, user, list(spec["slots"].values()), draft_look_id=spec["draft_look_id"])
                    by_id = {row["id"]: row for row in rows}
                    if spec["person_image_id"]:
                        person = connection.execute("SELECT path FROM person_images WHERE id=? AND owner_id=?", (spec["person_image_id"], user.owner_id)).fetchone()
                        if not person or person["path"] != spec["person_path"]:
                            raise ServiceError("形象照已改变，请重新确认", 409, "authorization_mismatch")
                    for kind, garment_id in spec["slots"].items():
                        if by_id[garment_id]["type"] != kind or self._hash(by_id[garment_id]["crop_path"]) != spec["current_hashes"][kind] or self._hash(spec["paths"][kind]) != spec["hashes"][kind]:
                            raise ServiceError("衣物已改变，请重新确认", 409, "authorization_mismatch")
                    if self._hash(spec["person_path"]) != spec["person_hash"]:
                        raise ServiceError("形象照已改变，请重新确认", 409, "authorization_mismatch")
                    estimate = self.usage.policy.estimate(f"tryon_{spec['quality']}")
                    if estimate > authorization["max_cents"]:
                        raise ServiceError("费用估算已改变，请重新确认", 409, "authorization_mismatch")
                    person_path = self._snapshot(spec["person_path"], "person", created)
                    snapshots = {kind: self._snapshot(path, kind, created) for kind, path in spec["paths"].items()}
                    if user.role == "visitor" and not self.reserve_visitor_quota(connection, user.owner_id):
                        raise ServiceError("本次演示额度已用完", 429, "demo_quota_exhausted")
                    tryon_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
                    connection.execute(
                        """INSERT INTO tryons(id,owner_id,person_path,upper_id,lower_id,dress_id,
                           upper_path,lower_path,dress_path,upper_label,lower_label,dress_label,draft_look_id,quality,status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')""",
                        (tryon_id, user.owner_id, person_path, *(spec["slots"].get(kind) for kind in ("upper", "lower", "dress")),
                         *(snapshots.get(kind) for kind in ("upper", "lower", "dress")),
                         *(spec["labels"].get(kind) for kind in ("upper", "lower", "dress")), spec["draft_look_id"], spec["quality"]))
                    connection.execute("INSERT INTO jobs(id,owner_id,kind,entity_id,status,stage,progress) VALUES (?,?,'tryon',?,'queued','queued',0)", (job_id,user.owner_id,tryon_id))
                    self.usage.reserve(job_id,user.owner_id,f"tryon_{spec['quality']}",step="tryon",connection=connection,
                                       metadata={"authorization_id": authorization_id,"max_cents":authorization["max_cents"]})
                    connection.execute("UPDATE tryon_authorizations SET consumed_job_id=?,consumed_tryon_id=? WHERE id=?", (job_id,tryon_id,authorization_id))
                    result = {"tryon_id":tryon_id,"job_id":job_id,"reused":False}
        except Exception as exc:
            for path in created:
                path.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise ServiceError("人物或衣物图片无法读取", 409, "source_missing") from exc
            raise
        if not reused:
            self.enqueue_tryon(result["job_id"], result["tryon_id"])
        return result

    def plus(self, user: UserContext, source_tryon_id: str, **options) -> dict:
        self._check_quality(user, "plus")
        return self.create(user, source_tryon_id=source_tryon_id, quality="plus", **options)

    def history(self, user: UserContext) -> list[dict]:
        with self.db() as connection:
            rows = connection.execute("SELECT * FROM tryons WHERE owner_id=? ORDER BY created_at DESC,id DESC", (user.owner_id,)).fetchall()
            items = []
            for row in rows:
                item = tryon_json(row)
                job = connection.execute("SELECT id,stage,error_message,remote_task_id FROM jobs WHERE entity_id=? AND kind='tryon' AND owner_id=? ORDER BY created_at DESC LIMIT 1",(row["id"],user.owner_id)).fetchone()
                if job:
                    item.update(job_id=job["id"],stage=job["stage"],error_message=job["error_message"],can_resume=bool(job["remote_task_id"]) and row["status"] != "failed" or row["status"] in {"queued","waiting_result"})
                items.append(item)
        return items
