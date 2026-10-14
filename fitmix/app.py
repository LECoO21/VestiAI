from __future__ import annotations

import io
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, abort, has_request_context, jsonify, render_template, request, send_from_directory, session
from PIL import Image, ImageOps, UnidentifiedImageError

from .bailian import BailianClient, BailianError, BailianSubmissionUnknown
from .backup import create_backup
from .services.context import ServiceError, UserContext
from .services.usage import UsageService, UsagePolicy, backfill_legacy_usage, SCHEMA_SQL as USAGE_SCHEMA
from .services.tryon import TryonService, SCHEMA_SQL as AUTHORIZATION_SCHEMA
from .services.wardrobe import (
    WardrobeService, duplicate_map, file_url, garment_json, look_json,
    person_image_json, thumbnail_url, tryon_json,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
ROOT = PACKAGE_ROOT.parent
DEPLOYMENT_PROFILE = os.environ.get("FITMIX_DEPLOYMENT_PROFILE", "").strip()
DEFAULT_DATA_DIR = Path("/tmp/fitmix-data") if DEPLOYMENT_PROFILE == "demo" else ROOT / "data"
DATA_DIR = Path(os.environ.get("WARDROBE_DATA_DIR", DEFAULT_DATA_DIR))
DB_PATH = DATA_DIR / "wardrobe.db"
CONFIG_PATH = Path(os.environ.get("FITMIX_CONFIG_PATH", ROOT / ".env"))
DESKTOP_MODE = os.environ.get("FITMIX_DESKTOP") == "1"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_IMAGE_BYTES = 5 * 1024
MAX_PERSON_IMAGES = 6
VALID_TYPES = {"upper", "lower", "dress"}
GENERIC_GARMENT_NAMES = {"单品", "上衣", "上装", "下装", "连体服", "连衣裙/连体服", "套装/连衣裙"}

COLOR_WORDS = (
    ("黑色", "黑"), ("白色", "白"), ("灰色", "灰"), ("米色", "米"),
    ("棕色", "棕"), ("咖色", "咖"), ("蓝色", "蓝"), ("藏青", "藏青"),
    ("绿色", "绿"), ("红色", "红"), ("粉色", "粉"), ("紫色", "紫"),
    ("黄色", "黄"), ("橙色", "橙"),
)
FEATURE_WORDS = (
    ("卡通鸭子", "鸭子"), ("星星", "星星"), ("竖条纹", "条纹"), ("横条纹", "条纹"),
    ("条纹", "条纹"), ("格纹", "格纹"), ("格子", "格纹"), ("印花", "印花"),
    ("刺绣", "刺绣"), ("直筒", "直筒"), ("阔腿", "阔腿"), ("短款", "短款"),
    ("宽松", "宽松"), ("圆领", "圆领"), ("V领", "V领"), ("翻领", "翻领"),
)
KIND_WORDS = (
    ("牛仔裤", "牛仔裤"), ("连衣裙", "连衣裙"), ("半身裙", "半裙"), ("短裙", "短裙"),
    ("衬衫", "衬衫"), ("卫衣", "卫衣"), ("T恤", "T恤"), ("针织衫", "针织衫"),
    ("毛衣", "毛衣"), ("夹克", "夹克"), ("西装", "西装"), ("外套", "外套"),
    ("短裤", "短裤"), ("长裤", "长裤"), ("裤", "长裤"), ("裙", "半裙"),
)

for folder in (DATA_DIR / "originals", DATA_DIR / "garment-sources", DATA_DIR / "garments", DATA_DIR / "person-images", DATA_DIR / "tryons", DATA_DIR / "thumbnails"):
    folder.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(DATA_DIR / "wardrobe.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("fitmix")

app = Flask(
    __name__,
    static_folder=str(PACKAGE_ROOT / "static"),
    template_folder=str(PACKAGE_ROOT / "templates"),
)
app.config.update(MAX_CONTENT_LENGTH=21 * 1024 * 1024, JSON_AS_ASCII=False)
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wardrobe-job")


def env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if CONFIG_PATH.exists():
        for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def runtime_value(name: str, default: str = "") -> str:
    return os.environ.get(name, env_values().get(name, default)).strip()


ACCESS_PASSWORD = runtime_value("FITMIX_ACCESS_PASSWORD")
ADMIN_PASSWORD = runtime_value("FITMIX_ADMIN_PASSWORD")
SESSION_SECRET = runtime_value("FITMIX_SECRET_KEY")
AUTH_ENABLED = bool(ACCESS_PASSWORD)
VISITOR_TRYON_LIMIT = int(runtime_value("FITMIX_VISITOR_TRYON_LIMIT", "3"))
VISITOR_BUDGET_CENTS = int(runtime_value("FITMIX_VISITOR_BUDGET_CENTS", "200"))
GLOBAL_BUDGET_CENTS = int(runtime_value("FITMIX_GLOBAL_BUDGET_CENTS", "6000"))
BASIC_TRYON_CENTS = 20


def validate_deployment_config(
    profile: str,
    data_dir: Path,
    access_password: str,
    admin_password: str,
    session_secret: str,
) -> None:
    if profile != "demo":
        return
    missing = [
        name
        for name, value in (
            ("FITMIX_ACCESS_PASSWORD", access_password),
            ("FITMIX_ADMIN_PASSWORD", admin_password),
            ("FITMIX_SECRET_KEY", session_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"演示部署缺少环境变量：{', '.join(missing)}")
    if access_password == admin_password:
        raise RuntimeError("访客密码和管理员密码不能相同")
    if len(session_secret) < 32:
        raise RuntimeError("FITMIX_SECRET_KEY 至少需要 32 个字符")
    if not data_dir.resolve().is_relative_to(Path("/tmp").resolve()):
        raise RuntimeError("veFaaS 演示数据目录必须位于 /tmp")


validate_deployment_config(
    DEPLOYMENT_PROFILE, DATA_DIR, ACCESS_PASSWORD, ADMIN_PASSWORD, SESSION_SECRET
)

app.secret_key = SESSION_SECRET or uuid.uuid4().hex
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=AUTH_ENABLED,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)


def current_owner_id() -> str:
    if not AUTH_ENABLED or not has_request_context():
        return "admin"
    return session.get("owner_id", "")


def current_role() -> str:
    if not AUTH_ENABLED:
        return "admin"
    return session.get("role", "")


def current_user() -> UserContext:
    if not has_request_context():
        raise ServiceError("业务操作需要由服务端提供用户身份", 401, "auth_required")
    return UserContext(current_owner_id(), current_role())


def wardrobe_service() -> WardrobeService:
    return WardrobeService(db, DATA_DIR)


def tryon_service() -> TryonService:
    return TryonService(db, DATA_DIR, reserve_visitor_tryon,
                        lambda job_id, tryon_id: enqueue(job_id, "tryon", tryon_id, process_tryon, tryon_id), usage_service())


def usage_policy(owner_id: str = "") -> UsagePolicy:
    estimates = {}
    for operation in ("recognition", "segmentation", "tryon_basic", "tryon_plus", "recommendation", "agent"):
        value = runtime_value(f"FITMIX_{operation.upper()}_CENTS")
        if value:
            estimates[operation] = int(value)
    return UsagePolicy(
        job_limit_cents=int(runtime_value("FITMIX_JOB_BUDGET_CENTS", "100")),
        user_limit_cents=min(int(runtime_value("FITMIX_USER_BUDGET_CENTS", "1000")), VISITOR_BUDGET_CENTS) if owner_id.startswith("visitor:") else int(runtime_value("FITMIX_USER_BUDGET_CENTS", "1000")),
        global_limit_cents=GLOBAL_BUDGET_CENTS, estimates=estimates,
        models={key:runtime_value(f"FITMIX_{key.upper()}_MODEL") for key in ("recommendation","agent") if runtime_value(f"FITMIX_{key.upper()}_MODEL")},
    )


def usage_service(owner_id: str = "") -> UsageService:
    return UsageService(db, usage_policy(owner_id or (current_owner_id() if has_request_context() else "")))


def save_desktop_credentials(workspace_id: str, api_key: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.{uuid.uuid4().hex}.tmp")
    values = env_values()
    values.update(DASHSCOPE_WORKSPACE_ID=workspace_id, DASHSCOPE_API_KEY=api_key)
    temporary.write_text("".join(f"{name}={value}\n" for name,value in values.items()), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, CONFIG_PATH)


def bailian_client() -> BailianClient:
    values = env_values()
    return BailianClient(
        os.environ.get("DASHSCOPE_API_KEY", values.get("DASHSCOPE_API_KEY", "")),
        os.environ.get("DASHSCOPE_WORKSPACE_ID", values.get("DASHSCOPE_WORKSPACE_ID", "")),
    )


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def image_fingerprint(path: Path) -> str | None:
    try:
        with Image.open(path) as image:
            pixels = list(image.convert("L").resize((8, 8)).get_flattened_data())
        average = sum(pixels) / len(pixels)
        return f"{sum((value >= average) << index for index, value in enumerate(pixels)):016x}"
    except (OSError, UnidentifiedImageError):
        return None


def garment_name_base(description: str, garment_type: str) -> str:
    description = description or ""
    colors = []
    found_colors = sorted(
        (description.index(source), short) for source, short in COLOR_WORDS if source in description
    )
    for _, short in found_colors:
        if short not in colors:
            colors.append(short)
    feature = next((short for source, short in FEATURE_WORDS if source in description), "")
    kind = next((short for source, short in KIND_WORDS if source in description), "")
    kind = kind or {"upper": "上衣", "lower": "下装", "dress": "连衣裙"}.get(garment_type, "单品")
    color = "".join(colors[:2])
    candidates = (color + feature + kind, colors[:1] and colors[0] + feature + kind, feature + kind, color + kind, kind)
    return next((name for name in candidates if name and len(name) <= 6), kind[:6])


def unique_short_name(base: str, used: set[str]) -> str:
    base = (base.strip() or "单品")[:6]
    if base not in used:
        return base
    for number in range(2, 10000):
        suffix = f"{number}号"
        candidate = f"{base[:max(1, 6 - len(suffix))]}{suffix}"
        if candidate not in used:
            return candidate
    raise RuntimeError("衣橱中的单品名称数量超过本地演示版上限")


def unique_garment_name(
    connection: sqlite3.Connection,
    description: str,
    garment_type: str,
    owner_id: str = "admin",
    preferred: str | None = None,
    exclude_id: str | None = None,
) -> str:
    sql = "SELECT label_zh FROM garments WHERE owner_id=? AND draft=0"
    params: list[str] = [owner_id]
    if exclude_id:
        sql += " AND id<>?"
        params.append(exclude_id)
    rows = connection.execute(sql, params).fetchall()
    used = {row["label_zh"] for row in rows}
    return unique_short_name(preferred or garment_name_base(description, garment_type), used)


SCHEMA_VERSION = 2


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    """Execute DDL inside the caller's transaction; executescript commits it."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("数据库迁移包含不完整的 SQL")


def _migrate_task_states(connection) -> None:
    # Rebuild only the old CHECK constraints; retain every column and record.
    for table in ("jobs", "tryons"):
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()["sql"]
        if "'unknown'" not in sql:
            replacement = re.sub(rf'CREATE TABLE\s+(?:"{table}"|{table})(?=\s*\()', f"CREATE TABLE {table}_v2", sql, count=1, flags=re.IGNORECASE)
            replacement = replacement.replace("'queued', 'running', 'succeeded', 'failed'", "'queued', 'running', 'succeeded', 'failed', 'unknown', 'waiting_result'")
            connection.execute(replacement)
            columns = ",".join(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
            connection.execute(f"INSERT INTO {table}_v2({columns}) SELECT {columns} FROM {table}")
            connection.execute(f"DROP TABLE {table}")
            connection.execute(f"ALTER TABLE {table}_v2 RENAME TO {table}")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    for name, definition in (("payload_json", "TEXT NOT NULL DEFAULT '{}'"), ("worker_pid", "INTEGER"), ("worker_token", "TEXT")):
        if name not in columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_entity ON jobs(entity_id)")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existed = DB_PATH.is_file()
    version = 0
    if existed:
        with sqlite3.connect(DB_PATH) as existing:
            version = existing.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("这份衣橱由较新的 VestiAI 创建，请使用对应版本打开")
        if version < SCHEMA_VERSION:
            backup = create_backup(DATA_DIR)
            logger.info("migration backup=%s", backup)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    created_files: list[Path] = []
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")
        _execute_statements(connection,
            """
            CREATE TABLE IF NOT EXISTS looks (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                source_path TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'photo' CHECK(source_kind IN ('photo', 'composition', 'tryon')),
                source_tryon_id TEXT,
                title_en TEXT,
                style_zh TEXT,
                seasons TEXT NOT NULL DEFAULT '[]',
                palette TEXT NOT NULL DEFAULT '[]',
                raw_analysis TEXT,
                draft INTEGER NOT NULL DEFAULT 0 CHECK(draft IN (0, 1)),
                favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS garments (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                look_id TEXT REFERENCES looks(id) ON DELETE SET NULL,
                source_kind TEXT NOT NULL DEFAULT 'look' CHECK(source_kind IN ('look', 'direct')),
                source_path TEXT,
                type TEXT NOT NULL CHECK(type IN ('upper', 'lower', 'dress')),
                label_zh TEXT NOT NULL,
                description_zh TEXT NOT NULL DEFAULT '',
                crop_path TEXT NOT NULL,
                mask_path TEXT,
                bbox TEXT,
                seasons TEXT NOT NULL DEFAULT '[]',
                image_hash TEXT,
                draft INTEGER NOT NULL DEFAULT 0 CHECK(draft IN (0, 1)),
                favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tryons (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                person_path TEXT NOT NULL,
                upper_id TEXT REFERENCES garments(id) ON DELETE SET NULL,
                lower_id TEXT REFERENCES garments(id) ON DELETE SET NULL,
                dress_id TEXT REFERENCES garments(id) ON DELETE SET NULL,
                upper_path TEXT,
                lower_path TEXT,
                dress_path TEXT,
                upper_label TEXT,
                lower_label TEXT,
                dress_label TEXT,
                draft_look_id TEXT,
                quality TEXT NOT NULL CHECK(quality IN ('basic', 'plus')),
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
                result_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
                stage TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                remote_task_id TEXT,
                remote_request_id TEXT,
                input_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS person_images (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'admin',
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS demo_usage (
                owner_id TEXT PRIMARY KEY,
                basic_tryons INTEGER NOT NULL DEFAULT 0,
                spent_cents INTEGER NOT NULL DEFAULT 0
            );
            """
        )

        look_columns = {row["name"] for row in connection.execute("PRAGMA table_info(looks)")}
        if "owner_id" not in look_columns:
            connection.execute("ALTER TABLE looks ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
        if "source_kind" not in look_columns:
            connection.execute("ALTER TABLE looks ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'photo'")
        if "source_tryon_id" not in look_columns:
            connection.execute("ALTER TABLE looks ADD COLUMN source_tryon_id TEXT")
        if "favorite" not in look_columns:
            connection.execute("ALTER TABLE looks ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
        if "draft" not in look_columns:
            connection.execute("ALTER TABLE looks ADD COLUMN draft INTEGER NOT NULL DEFAULT 0")

        job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "owner_id" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
        if "input_path" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN input_path TEXT")

        garment_columns = {row["name"] for row in connection.execute("PRAGMA table_info(garments)")}
        if "owner_id" not in garment_columns:
            connection.execute("ALTER TABLE garments ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
            garment_columns.add("owner_id")
        if "draft" not in garment_columns:
            connection.execute("ALTER TABLE garments ADD COLUMN draft INTEGER NOT NULL DEFAULT 0")
            garment_columns.add("draft")
        garment_fk = next((row for row in connection.execute("PRAGMA foreign_key_list(garments)") if row["from"] == "look_id"), None)
        garment_schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='garments'").fetchone()["sql"]
        if "image_hash" not in garment_columns or (garment_fk and garment_fk["on_delete"] != "SET NULL") or "'shoes'" in garment_schema or "'hat'" in garment_schema:
            if "source_kind" in garment_columns:
                garment_select = """g.id, g.owner_id, g.look_id, g.source_kind, g.source_path, g.type, g.label_zh, g.description_zh,
                       g.crop_path, g.mask_path, g.bbox, g.seasons, NULL, g.draft, g.favorite, g.created_at
                FROM garments g"""
            else:
                garment_select = """g.id, g.owner_id, g.look_id, 'look', NULL, g.type, g.label_zh, g.description_zh,
                       g.crop_path, g.mask_path, g.bbox, COALESCE(l.seasons, '[]'), NULL, 0, 0, g.created_at
                FROM garments g JOIN looks l ON l.id=g.look_id"""
            _execute_statements(connection,
                f"""
                CREATE TABLE garments_new (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT 'admin',
                    look_id TEXT REFERENCES looks(id) ON DELETE SET NULL,
                    source_kind TEXT NOT NULL DEFAULT 'look' CHECK(source_kind IN ('look', 'direct')),
                    source_path TEXT,
                    type TEXT NOT NULL CHECK(type IN ('upper', 'lower', 'dress')),
                    label_zh TEXT NOT NULL,
                    description_zh TEXT NOT NULL DEFAULT '',
                    crop_path TEXT NOT NULL,
                    mask_path TEXT,
                    bbox TEXT,
                    seasons TEXT NOT NULL DEFAULT '[]',
                    image_hash TEXT,
                    draft INTEGER NOT NULL DEFAULT 0 CHECK(draft IN (0, 1)),
                    favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO garments_new
                    (id, owner_id, look_id, source_kind, source_path, type, label_zh, description_zh,
                     crop_path, mask_path, bbox, seasons, image_hash, draft, favorite, created_at)
                SELECT {garment_select};
                DROP TABLE garments;
                ALTER TABLE garments_new RENAME TO garments;
                """
            )

        tryon_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tryons)")}
        if "owner_id" not in tryon_columns:
            connection.execute("ALTER TABLE tryons ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
            tryon_columns.add("owner_id")
        if "draft_look_id" not in tryon_columns:
            connection.execute("ALTER TABLE tryons ADD COLUMN draft_look_id TEXT")
            tryon_columns.add("draft_look_id")
        tryon_fks = {row["from"]: row["on_delete"] for row in connection.execute("PRAGMA foreign_key_list(tryons)")}
        if "upper_path" not in tryon_columns or any(tryon_fks.get(key) != "SET NULL" for key in ("upper_id", "lower_id", "dress_id")):
            _execute_statements(connection,
                """
                CREATE TABLE tryons_new (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT 'admin',
                    person_path TEXT NOT NULL,
                    upper_id TEXT REFERENCES garments(id) ON DELETE SET NULL,
                    lower_id TEXT REFERENCES garments(id) ON DELETE SET NULL,
                    dress_id TEXT REFERENCES garments(id) ON DELETE SET NULL,
                    upper_path TEXT,
                    lower_path TEXT,
                    dress_path TEXT,
                    upper_label TEXT,
                    lower_label TEXT,
                    dress_label TEXT,
                    draft_look_id TEXT,
                    quality TEXT NOT NULL CHECK(quality IN ('basic', 'plus')),
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
                    result_path TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tryons_new
                    (id, owner_id, person_path, upper_id, lower_id, dress_id, upper_path, lower_path, dress_path,
                     upper_label, lower_label, dress_label, draft_look_id, quality, status, result_path, created_at)
                SELECT t.id, t.owner_id, t.person_path, t.upper_id, t.lower_id, t.dress_id,
                       u.crop_path, l.crop_path, d.crop_path, u.label_zh, l.label_zh, d.label_zh,
                       t.draft_look_id, t.quality, t.status, t.result_path, t.created_at
                FROM tryons t
                LEFT JOIN garments u ON u.id=t.upper_id
                LEFT JOIN garments l ON l.id=t.lower_id
                LEFT JOIN garments d ON d.id=t.dress_id;
                DROP TABLE tryons;
                ALTER TABLE tryons_new RENAME TO tryons;
                """
            )

        person_columns = {row["name"] for row in connection.execute("PRAGMA table_info(person_images)")}
        person_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='person_images'"
        ).fetchone()["sql"]
        if "owner_id" not in person_columns:
            connection.execute("ALTER TABLE person_images ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
        if "NAME TEXT NOT NULL UNIQUE" in person_schema.upper():
            _execute_statements(connection,
                """
                CREATE TABLE person_images_new (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT 'admin',
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO person_images_new(id, owner_id, name, path, created_at)
                    SELECT id, owner_id, name, path, created_at FROM person_images;
                DROP TABLE person_images;
                ALTER TABLE person_images_new RENAME TO person_images;
                """
            )

        _execute_statements(connection,
            """
            CREATE TABLE IF NOT EXISTS look_garments (
                look_id TEXT NOT NULL REFERENCES looks(id) ON DELETE CASCADE,
                garment_id TEXT NOT NULL REFERENCES garments(id) ON DELETE CASCADE,
                PRIMARY KEY (look_id, garment_id)
            );
            INSERT OR IGNORE INTO look_garments(look_id, garment_id)
                SELECT look_id, id FROM garments WHERE look_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_garments_look ON garments(look_id);
            CREATE INDEX IF NOT EXISTS idx_garments_type ON garments(type);
            CREATE INDEX IF NOT EXISTS idx_look_garments_garment ON look_garments(garment_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_entity ON jobs(entity_id);
            CREATE INDEX IF NOT EXISTS idx_person_images_created ON person_images(created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_person_images_owner_name ON person_images(owner_id, name);
            """
        )

        for row in connection.execute("SELECT * FROM tryons"):
            updates = {}
            for garment_type in ("upper", "lower", "dress"):
                stored = row[f"{garment_type}_path"]
                if stored and stored.startswith("garments/") and (DATA_DIR / stored).exists():
                    suffix = (DATA_DIR / stored).suffix or ".png"
                    destination = DATA_DIR / "tryons" / f"{uuid.uuid4().hex}-{garment_type}{suffix}"
                    shutil.copy2(DATA_DIR / stored, destination)
                    created_files.append(destination)
                    updates[f"{garment_type}_path"] = rel_path(destination)
            if updates:
                assignments = ", ".join(f"{key}=?" for key in updates)
                connection.execute(f"UPDATE tryons SET {assignments} WHERE id=?", (*updates.values(), row["id"]))

        for row in connection.execute("SELECT id, crop_path FROM garments WHERE image_hash IS NULL"):
            fingerprint = image_fingerprint(DATA_DIR / row["crop_path"])
            if fingerprint:
                connection.execute("UPDATE garments SET image_hash=? WHERE id=?", (fingerprint, row["id"]))

        garment_rows = connection.execute(
            "SELECT id, owner_id, type, label_zh, description_zh FROM garments WHERE draft=0 ORDER BY created_at, id"
        ).fetchall()
        reserved: dict[str, set[str]] = {}
        for row in connection.execute("SELECT owner_id, label_zh FROM garments WHERE draft=0"):
            label = row["label_zh"].strip()
            if label not in GENERIC_GARMENT_NAMES and len(label) <= 6:
                reserved.setdefault(row["owner_id"], set()).add(label)
        used: dict[str, set[str]] = {}
        for row in garment_rows:
            owner_id = row["owner_id"]
            current = row["label_zh"].strip()
            owner_used = used.setdefault(owner_id, set())
            if current in GENERIC_GARMENT_NAMES or len(current) > 6 or current in owner_used:
                current = unique_short_name(
                    garment_name_base(row["description_zh"], row["type"]),
                    reserved.get(owner_id, set()) | owner_used,
                )
                connection.execute("UPDATE garments SET label_zh=? WHERE id=?", (current, row["id"]))
            owner_used.add(current)
        connection.execute("DROP INDEX IF EXISTS idx_garments_unique_name")
        connection.execute("DROP INDEX IF EXISTS idx_garments_owner_name")
        connection.execute(
            "CREATE UNIQUE INDEX idx_garments_owner_name ON garments(owner_id, label_zh) WHERE draft=0"
        )

        _migrate_task_states(connection)
        _execute_statements(connection, AUTHORIZATION_SCHEMA)
        if "confirmed" not in {row["name"] for row in connection.execute("PRAGMA table_info(tryon_authorizations)")}:
            connection.execute("ALTER TABLE tryon_authorizations ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
        _execute_statements(connection, USAGE_SCHEMA)
        if version < 2:
            backfill_legacy_usage(connection,usage_policy())
        broken = connection.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"数据库迁移后存在无效关联：{broken}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        for path in created_files:
            path.unlink(missing_ok=True)
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.close()


def api_error(message: str, status: int = 400, code: str = "bad_request"):
    return jsonify({"error": {"code": code, "message": message}}), status


def rel_path(path: Path) -> str:
    return path.relative_to(DATA_DIR).as_posix()


def save_upload(upload, category: str) -> str:
    if not upload or not upload.filename:
        raise ValueError("请选择 JPG 或 PNG 图片")
    content = upload.read(MAX_IMAGE_BYTES + 1)
    if len(content) < MIN_IMAGE_BYTES or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("图片大小需在 5KB–20MB 之间")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.format not in {"JPEG", "PNG"}:
                raise ValueError("只接受 JPG 或 PNG 图片")
            width, height = image.size
            if min(width, height) < 150 or max(width, height) > 8192:
                raise ValueError("图片边长需在 150–8192px 之间")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            extension = ".jpg" if image.format == "JPEG" else ".png"
            destination = DATA_DIR / category / f"{uuid.uuid4().hex}{extension}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if extension == ".jpg":
                image.convert("RGB").save(destination, "JPEG", quality=95)
            else:
                mode = "RGBA" if "A" in image.getbands() else "RGB"
                image.convert(mode).save(destination, "PNG", optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("图片文件已损坏或格式不受支持") from exc
    return rel_path(destination)


def create_job(kind: str, entity_id: str, input_path: str | None = None, owner_id: str | None = None, payload: dict | None = None) -> str:
    job_id = uuid.uuid4().hex
    owner_id = owner_id or current_owner_id()
    payload = payload or {}
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM jobs WHERE owner_id=? AND entity_id=? AND status IN ('queued','running','unknown','waiting_result') ORDER BY created_at DESC LIMIT 1",(owner_id,entity_id)).fetchone()
        if existing:
            if existing["kind"] == kind and json.loads(existing["payload_json"]) == payload:
                return existing["id"]
            raise ServiceError("原任务尚未完成，请先查看或核实原任务",409,"active_task")
        connection.execute(
            """INSERT INTO jobs(id, owner_id, kind, entity_id, status, stage, progress, input_path, payload_json)
               VALUES (?, ?, ?, ?, 'queued', 'queued', 0, ?, ?)""",
            (job_id,owner_id,kind,entity_id,input_path,json.dumps(payload)))
        operations = ["segmentation"]
        if kind in {"look","tryon_draft"} or kind == "garment" and not payload.get("forced_type"):
            operations.insert(0,"recognition")
        if kind in {"look","tryon_draft","garment","parse","garment_parse"}:
            for operation in operations:
                usage_service(owner_id).reserve(job_id,owner_id,operation,connection=connection)
    return job_id


def set_job(job_id: str, **fields) -> None:
    allowed = {
        "status",
        "stage",
        "progress",
        "error_code",
        "error_message",
        "remote_task_id",
        "remote_request_id",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key}=?" for key in values)
    with db() as connection:
        connection.execute(
            f"UPDATE jobs SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (*values.values(), job_id),
        )


WORKER_TOKEN = uuid.uuid4().hex
_recovery_lock = threading.Lock()
_recovery_started = False


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _job_state(job_id: str, state: str, stage: str, message=None, code=None) -> None:
    with db() as connection:
        connection.execute("UPDATE jobs SET status=?,stage=?,error_message=?,error_code=?,worker_pid=NULL,worker_token=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (state,stage,message,code,job_id))
        connection.execute("UPDATE tryons SET status=? WHERE id=(SELECT entity_id FROM jobs WHERE id=? AND kind='tryon')", (state,job_id))


def paid_call(job_id: str, operation: str, step: str, client, function, *args, async_task=False):
    """Cache completed paid steps; an uncertain POST is never automatically sent again."""
    with db() as connection:
        job = connection.execute("SELECT owner_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise ServiceError("任务不存在",404,"not_found")
    service = usage_service(job["owner_id"])
    record = service.reserve(job_id,job["owner_id"],operation,step)
    if record["status"] == "succeeded" and "result" in record["usage"]:
        return record["usage"]["result"]
    if record["status"] != "reserved":
        raise BailianSubmissionUnknown("这个收费步骤已提交，请核实原请求，未重复提交")
    attempted = False
    def on_submitting():
        nonlocal attempted
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT status FROM model_usage WHERE id=?", (record["id"],)).fetchone()
            if not current or current["status"] != "reserved":
                raise ServiceError("原请求已进入提交，请查看原任务",409,"call_in_progress")
            service.settle(record["id"],"submitted",connection=connection)
            connection.execute("UPDATE jobs SET stage=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (f"submitting_{step}",job_id))
        attempted = True
    try:
        result = function(*args,on_submitting=on_submitting)
    except Exception as exc:
        if isinstance(exc,ServiceError) and exc.code == "call_in_progress":
            raise BailianSubmissionUnknown(str(exc)) from exc
        state = "unknown" if isinstance(exc,BailianSubmissionUnknown) else "failed" if attempted else "released"
        service.settle(record["id"],state,usage={"reported_usage":getattr(client,"last_usage",{"availability":"not_reported"})})
        raise
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        service.settle(record["id"],"submitted" if async_task else "succeeded",
                       usage={"result":result,"reported_usage":getattr(client,"last_usage",{"availability":"not_reported"}),
                              "input_images": (1 if operation in {"recognition","segmentation"} else sum(arg is not None for arg in args[:3]))},
                       connection=connection)
        if async_task:
            connection.execute("UPDATE jobs SET remote_task_id=?,remote_request_id=?,stage='generating',progress=45 WHERE id=?",(result[0],result[1],job_id))
    return result


def run_job(job_id: str, kind: str, entity_id: str, function, *args) -> None:
    started = time.monotonic()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        claimed = connection.execute("UPDATE jobs SET status='running',worker_pid=?,worker_token=?,error_code=NULL,error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'",(os.getpid(),WORKER_TOKEN,job_id)).rowcount
    if not claimed:
        return
    logger.info("job=%s kind=%s entity=%s started",job_id,kind,entity_id)
    try:
        function(job_id,*args)
        _job_state(job_id,"succeeded","done")
        set_job(job_id,progress=100)
    except Exception as exc:
        code = exc.code if isinstance(exc,(BailianError,ServiceError)) else "internal_error"
        message = str(exc)[:300] or "任务尚未完成"
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for pending in connection.execute("SELECT id FROM model_usage WHERE job_id=? AND status='reserved'",(job_id,)).fetchall():
                usage_service().settle(pending["id"],"released",connection=connection)
            if code == "tryon_failed":
                for submitted in connection.execute("SELECT id FROM model_usage WHERE job_id=? AND status IN ('submitted','unknown')",(job_id,)).fetchall():
                    usage_service().settle(submitted["id"],"failed",connection=connection)
            row = connection.execute("SELECT remote_task_id FROM jobs WHERE id=?",(job_id,)).fetchone()
            steps = connection.execute("SELECT status FROM model_usage WHERE job_id=?",(job_id,)).fetchall()
        if isinstance(exc,BailianSubmissionUnknown) or code == "task_unknown" or any(item["status"] in {"unknown","submitted"} for item in steps) and not (row and row["remote_task_id"]):
            state,stage = "unknown","pending_verification"
            message += "；提交结果待核实，未重新生成"
        elif row and row["remote_task_id"] and code != "tryon_failed":
            state,stage = "waiting_result","waiting_result"
            message += "；可继续查询原任务，不会再次生成"
        else:
            state,stage = "failed","failed"
        _job_state(job_id,state,stage,message,code)
        if isinstance(exc,BailianError) and exc.request_id:
            set_job(job_id,remote_request_id=exc.request_id)
        logger.warning("job=%s kind=%s state=%s code=%s",job_id,kind,state,code)


def enqueue(job_id: str, kind: str, entity_id: str, function, *args) -> None:
    try:
        executor.submit(run_job,job_id,kind,entity_id,function,*args)
    except RuntimeError:
        _job_state(job_id,"waiting_result","queue_unavailable","后台暂时无法启动，可继续处理原任务","queue_unavailable")


def _dispatch(row) -> None:
    payload = json.loads(row["payload_json"] or "{}")
    kind,entity = row["kind"],row["entity_id"]
    if kind == "tryon":
        function,args = process_tryon,(entity,)
    elif kind in {"look","tryon_draft"}:
        function,args = process_look,(entity,)
    elif kind == "garment" and row["input_path"]:
        function,args = process_direct_garment,(entity,row["input_path"],payload.get("forced_type"),row["owner_id"])
    elif kind == "parse" and payload.get("types"):
        function,args = process_manual_parse,(entity,payload["types"])
    elif kind == "garment_parse" and payload.get("type"):
        function,args = process_reparse_garment,(entity,payload["type"])
    else:
        _job_state(row["id"],"unknown","pending_verification","旧任务缺少恢复参数，请核实结果；未重新提交","recovery_missing")
        return
    enqueue(row["id"],kind,entity,function,*args)


def recover_jobs() -> None:
    dispatch = []
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT * FROM jobs WHERE status IN ('queued','running','waiting_result')").fetchall()
        for row in rows:
            if _alive(row["worker_pid"]):
                continue
            steps = connection.execute("SELECT status FROM model_usage WHERE job_id=?",(row["id"],)).fetchall()
            uncertain = any(step["status"] in {"submitted","unknown"} for step in steps)
            if not row["remote_task_id"] and (uncertain or row["stage"].startswith("submitting") or row["status"] == "running" and not steps):
                connection.execute("UPDATE jobs SET status='unknown',stage='pending_verification',worker_pid=NULL,worker_token=NULL,error_code='submission_unknown',error_message='上次提交结果待核实，未重新生成' WHERE id=?",(row["id"],))
                connection.execute("UPDATE tryons SET status='unknown' WHERE id=? AND ?='tryon'",(row["entity_id"],row["kind"]))
                for step in connection.execute("SELECT id,status FROM model_usage WHERE job_id=? AND status IN ('reserved','submitted')",(row["id"],)).fetchall():
                    usage_service().settle(step["id"],"unknown",connection=connection)
                continue
            connection.execute("UPDATE jobs SET status='queued',worker_pid=NULL,worker_token=NULL WHERE id=?",(row["id"],))
            dispatch.append(dict(row))
    for row in dispatch:
        _dispatch(row)


@app.before_request
def recover_once():
    global _recovery_started
    if runtime_value("FITMIX_RECOVERY_DISABLED") == "1" or _recovery_started:
        return
    with _recovery_lock:
        if not _recovery_started:
            recover_jobs()
            _recovery_started = True


def matching_garment_id(
    connection: sqlite3.Connection,
    owner_id: str,
    garment_type: str,
    fingerprint: str | None,
    exclude_id: str | None = None,
) -> str | None:
    if not fingerprint:
        return None
    rows = connection.execute(
        "SELECT id, image_hash FROM garments WHERE owner_id=? AND type=? AND draft=0 AND image_hash IS NOT NULL",
        (owner_id, garment_type),
    ).fetchall()
    return next(
        (
            row["id"] for row in rows
            if row["id"] != exclude_id
            and (int(fingerprint, 16) ^ int(row["image_hash"], 16)).bit_count() <= 4
        ),
        None,
    )


def store_parsed_garments(job_id: str, look_id: str, specs: list[dict], replace_types: list[str]) -> None:
    client = bailian_client()
    with db() as connection:
        look = connection.execute(
            "SELECT owner_id, source_path, seasons, draft FROM looks WHERE id=?", (look_id,)
        ).fetchone()
        placeholders = ",".join("?" for _ in replace_types)
        old_rows = connection.execute(
            f"SELECT * FROM garments WHERE owner_id=? AND look_id=? AND type IN ({placeholders})",
            (look["owner_id"], look_id, *replace_types) if look else ("", look_id, *replace_types),
        ).fetchall()
    if not look:
        raise BailianError("not_found", "穿搭记录不存在")
    old_by_type = {row["type"]: row for row in old_rows}
    source = DATA_DIR / look["source_path"]
    set_job(job_id, stage="parsing", progress=45)
    parsed = paid_call(job_id,"segmentation","segmentation",client,client.parse_garments,source,replace_types)
    spec_by_type = {item["type"]: item for item in specs}
    new_rows = []
    for index, item in enumerate(parsed):
        set_job(job_id, stage="downloading_garments", progress=55 + int(25 * (index + 1) / len(parsed)))
        garment_id = old_by_type[item["type"]]["id"] if item["type"] in old_by_type else uuid.uuid4().hex
        file_token = uuid.uuid4().hex
        crop_path = DATA_DIR / "garments" / f"{file_token}-rgb.png"
        mask_path = DATA_DIR / "garments" / f"{file_token}-rgba.png" if item.get("parsing_url") else None
        client.download(item["crop_url"], crop_path)
        if mask_path:
            client.download(item["parsing_url"], mask_path)
        garment_type = item["type"]
        spec = spec_by_type.get(garment_type, {})
        new_rows.append(
            (
                garment_id,
                look_id,
                garment_type,
                spec.get("label_zh") or {"upper": "上衣", "lower": "下装", "dress": "套装/连衣裙"}[garment_type],
                spec.get("description_zh", "手动选择类型后重新分割"),
                rel_path(crop_path),
                rel_path(mask_path) if mask_path else None,
                json.dumps(item.get("bbox"), ensure_ascii=False),
                look["seasons"],
                image_fingerprint(crop_path),
            )
        )

    old_files = [
        path
        for row in old_rows
        for path in (row["crop_path"], row["mask_path"])
        if path
    ]
    discarded_files: list[str] = []
    with db() as connection:
        for row in new_rows:
            name = unique_garment_name(
                connection, row[4], row[2], look["owner_id"], exclude_id=row[0]
            )
            if row[2] in old_by_type:
                connection.execute(
                    """UPDATE garments SET label_zh=?, description_zh=?, crop_path=?, mask_path=?, bbox=?, seasons=?, image_hash=?
                       WHERE id=?""",
                    (name, row[4], row[5], row[6], row[7], row[8], row[9], row[0]),
                )
            else:
                duplicate_id = matching_garment_id(
                    connection, look["owner_id"], row[2], row[9], row[0]
                )
                if duplicate_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO look_garments(look_id, garment_id) VALUES (?, ?)",
                        (look_id, duplicate_id),
                    )
                    discarded_files.extend(path for path in (row[5], row[6]) if path)
                    continue
                connection.execute(
                    """INSERT INTO garments
                       (id, owner_id, look_id, type, label_zh, description_zh, crop_path, mask_path, bbox, seasons, image_hash, draft)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row[0], look["owner_id"], row[1], row[2], name, *row[4:], look["draft"]),
                )
            connection.execute(
                "INSERT OR IGNORE INTO look_garments(look_id, garment_id) VALUES (?, ?)",
                (look_id, row[0]),
            )
    for stored in old_files:
        (DATA_DIR / stored).unlink(missing_ok=True)
    for stored in discarded_files:
        (DATA_DIR / stored).unlink(missing_ok=True)


def process_look(job_id: str, look_id: str) -> None:
    with db() as connection:
        look = connection.execute("SELECT source_path, source_kind FROM looks WHERE id=?", (look_id,)).fetchone()
    if not look:
        raise BailianError("not_found", "穿搭记录不存在")
    set_job(job_id, stage="analyzing", progress=10)
    client = bailian_client()
    analysis = paid_call(job_id,"recognition","recognition",client,client.analyze_look,DATA_DIR / look["source_path"],look["source_kind"])
    with db() as connection:
        connection.execute(
            """UPDATE looks SET title_en=?, style_zh=?, seasons=?, palette=?, raw_analysis=? WHERE id=?""",
            (
                analysis["title_en"],
                analysis["style_zh"],
                json.dumps(analysis["seasons"], ensure_ascii=False),
                json.dumps(analysis["palette"], ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False),
                look_id,
            ),
        )
    garment_types = [item["type"] for item in analysis["garments"]]
    store_parsed_garments(job_id, look_id, analysis["garments"], garment_types)


def materialize_draft_look(look_id: str, owner_id: str, publish_look: bool) -> tuple[dict[str, str], dict | None]:
    """Promote a temporary uploaded outfit into the library or wardrobe."""
    with db() as connection:
        look = connection.execute(
            "SELECT * FROM looks WHERE id=? AND owner_id=?", (look_id, owner_id)
        ).fetchone()
        rows = connection.execute(
            """SELECT g.* FROM garments g JOIN look_garments lg ON lg.garment_id=g.id
               WHERE lg.look_id=? AND g.owner_id=? ORDER BY g.created_at, g.type""",
            (look_id, owner_id),
        ).fetchall()
    if not look or not look["draft"]:
        return {}, None
    if not rows:
        raise ValueError("这张搭配图还没有识别出可使用的衣服")

    source_copy: Path | None = None
    source_copy_used = False
    if not publish_look and any(row["draft"] for row in rows):
        source = DATA_DIR / look["source_path"]
        if not source.exists():
            raise ValueError("临时搭配图已不存在，请重新上传")
        source_copy = DATA_DIR / "garment-sources" / f"{uuid.uuid4().hex}{source.suffix or '.png'}"
        shutil.copy2(source, source_copy)

    promoted: dict[str, str] = {}
    discarded: list[str] = []
    try:
        with db() as connection:
            for garment in rows:
                if not garment["draft"]:
                    promoted[garment["type"]] = garment["id"]
                    continue
                duplicate_id = matching_garment_id(
                    connection, owner_id, garment["type"], garment["image_hash"], garment["id"]
                )
                if duplicate_id:
                    connection.execute(
                        "INSERT OR IGNORE INTO look_garments(look_id, garment_id) VALUES (?, ?)",
                        (look_id, duplicate_id),
                    )
                    connection.execute(
                        f"UPDATE tryons SET {garment['type']}_id=? WHERE {garment['type']}_id=? AND owner_id=?",
                        (duplicate_id, garment["id"], owner_id),
                    )
                    connection.execute("DELETE FROM garments WHERE id=?", (garment["id"],))
                    discarded.extend(
                        stored for stored in (garment["crop_path"], garment["mask_path"]) if stored
                    )
                    promoted[garment["type"]] = duplicate_id
                    continue
                name = unique_garment_name(
                    connection,
                    garment["description_zh"],
                    garment["type"],
                    owner_id,
                    preferred=garment["label_zh"],
                    exclude_id=garment["id"],
                )
                if publish_look:
                    connection.execute(
                        "UPDATE garments SET draft=0, label_zh=? WHERE id=?",
                        (name, garment["id"]),
                    )
                else:
                    connection.execute(
                        """UPDATE garments SET draft=0, look_id=NULL, source_kind='direct', source_path=?, label_zh=?
                           WHERE id=?""",
                        (rel_path(source_copy), name, garment["id"]),
                    )
                    source_copy_used = True
                promoted[garment["type"]] = garment["id"]

            if publish_look:
                connection.execute("UPDATE looks SET draft=0 WHERE id=?", (look_id,))
                saved = connection.execute("SELECT * FROM looks WHERE id=?", (look_id,)).fetchone()
                result = look_json(connection, saved)
            else:
                connection.execute("DELETE FROM jobs WHERE entity_id=? AND owner_id=?", (look_id, owner_id))
                connection.execute("DELETE FROM looks WHERE id=? AND owner_id=?", (look_id, owner_id))
                result = None
    except Exception:
        if source_copy:
            source_copy.unlink(missing_ok=True)
        raise

    for stored in discarded:
        (DATA_DIR / stored).unlink(missing_ok=True)
    if source_copy and not source_copy_used:
        source_copy.unlink(missing_ok=True)
    if not publish_look:
        (DATA_DIR / look["source_path"]).unlink(missing_ok=True)
    return promoted, result


def process_manual_parse(job_id: str, look_id: str, garment_types: list[str]) -> None:
    with db() as connection:
        row = connection.execute("SELECT raw_analysis FROM looks WHERE id=?", (look_id,)).fetchone()
    analysis = json.loads(row["raw_analysis"] or "{}") if row else {}
    specs = analysis.get("garments", [])
    store_parsed_garments(job_id, look_id, specs, garment_types)


def process_direct_garment(
    job_id: str,
    garment_id: str,
    stored_source: str,
    forced_type: str | None = None,
    owner_id: str = "admin",
) -> None:
    source = DATA_DIR / stored_source
    created_files: list[Path] = []
    try:
        set_job(job_id, stage="analyzing_garment", progress=12)
        client = bailian_client()
        if forced_type:
            specs = [{
                "type": forced_type,
                "label_zh": {"upper": "上衣", "lower": "下装", "dress": "套装/连衣裙"}[forced_type],
                "description_zh": "用户手动指定类型后重新分割",
            }]
            seasons = []
        else:
            analysis = paid_call(job_id,"recognition","recognition",client,client.analyze_look,source,"wardrobe")
            specs = analysis["garments"]
            seasons = analysis["seasons"]
        set_job(job_id, stage="parsing", progress=45)
        parsed_items = paid_call(job_id,"segmentation","segmentation",client,client.parse_garments,source,[item["type"] for item in specs])
        spec_by_type = {item["type"]: item for item in specs}
        prepared = []
        for index, parsed in enumerate(parsed_items):
            set_job(
                job_id,
                stage="downloading_garments",
                progress=55 + int(30 * (index + 1) / len(parsed_items)),
            )
            crop_path = DATA_DIR / "garments" / f"{uuid.uuid4().hex}-rgb.png"
            mask_path = DATA_DIR / "garments" / f"{uuid.uuid4().hex}-rgba.png" if parsed.get("parsing_url") else None
            client.download(parsed["crop_url"], crop_path)
            created_files.append(crop_path)
            if mask_path:
                client.download(parsed["parsing_url"], mask_path)
                created_files.append(mask_path)
            spec = spec_by_type[parsed["type"]]
            prepared.append((
                parsed["type"], spec["description_zh"], crop_path, mask_path,
                json.dumps(parsed.get("bbox"), ensure_ascii=False), image_fingerprint(crop_path),
            ))
        kept = 0
        with db() as connection:
            for index, item in enumerate(prepared):
                garment_type, description, crop_path, mask_path, bbox, fingerprint = item
                if matching_garment_id(connection, owner_id, garment_type, fingerprint):
                    continue
                name = unique_garment_name(connection, description, garment_type, owner_id)
                connection.execute(
                    """INSERT INTO garments
                       (id, owner_id, source_kind, source_path, type, label_zh, description_zh,
                        crop_path, mask_path, bbox, seasons, image_hash)
                       VALUES (?, ?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        garment_id if kept == 0 else uuid.uuid4().hex,
                        owner_id,
                        stored_source,
                        garment_type,
                        name,
                        description,
                        rel_path(crop_path),
                        rel_path(mask_path) if mask_path else None,
                        bbox,
                        json.dumps(seasons, ensure_ascii=False),
                        fingerprint,
                    ),
                )
                kept += 1
        with db() as connection:
            rows = connection.execute(
                "SELECT crop_path, mask_path FROM garments WHERE owner_id=? AND source_path=?",
                (owner_id, stored_source),
            ).fetchall()
            referenced = {stored for row in rows for stored in (row["crop_path"], row["mask_path"]) if stored}
        if not kept and not rows:
            source.unlink(missing_ok=True)
        for path in created_files:
            if rel_path(path) not in referenced:
                path.unlink(missing_ok=True)
    except Exception:
        for path in created_files:
            path.unlink(missing_ok=True)
        raise


def process_reparse_garment(job_id: str, garment_id: str, garment_type: str) -> None:
    with db() as connection:
        garment = connection.execute(
            """SELECT g.*, l.source_path AS look_source
               FROM garments g LEFT JOIN looks l ON l.id=g.look_id WHERE g.id=?""",
            (garment_id,),
        ).fetchone()
    if not garment:
        raise BailianError("not_found", "单品不存在")
    stored_source = garment["source_path"] or garment["look_source"]
    if not stored_source or not (DATA_DIR / stored_source).exists():
        raise BailianError("source_missing", "原始图片已经不存在，请重新添加这件单品")
    set_job(job_id, stage="parsing", progress=35)
    client = bailian_client()
    parsed = paid_call(job_id,"segmentation","segmentation",client,client.parse_garments,DATA_DIR / stored_source,[garment_type])[0]
    crop_path = DATA_DIR / "garments" / f"{uuid.uuid4().hex}-rgb.png"
    mask_path = DATA_DIR / "garments" / f"{uuid.uuid4().hex}-rgba.png" if parsed.get("parsing_url") else None
    client.download(parsed["crop_url"], crop_path)
    if mask_path:
        client.download(parsed["parsing_url"], mask_path)
    with db() as connection:
        label = unique_garment_name(
            connection, garment["description_zh"], garment_type, garment["owner_id"], exclude_id=garment_id
        )
        connection.execute(
            """UPDATE garments SET type=?, label_zh=?, crop_path=?, mask_path=?, bbox=?, image_hash=? WHERE id=?""",
            (
                garment_type,
                label,
                rel_path(crop_path),
                rel_path(mask_path) if mask_path else None,
                json.dumps(parsed.get("bbox"), ensure_ascii=False),
                image_fingerprint(crop_path),
                garment_id,
            ),
        )
    for stored in (garment["crop_path"], garment["mask_path"]):
        if stored:
            (DATA_DIR / stored).unlink(missing_ok=True)


def process_tryon(job_id: str, tryon_id: str) -> None:
    with db() as connection:
        row = connection.execute("SELECT * FROM tryons WHERE id=?",(tryon_id,)).fetchone()
        job = connection.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        connection.execute("UPDATE tryons SET status='running' WHERE id=?",(tryon_id,))
    if not row or not job:
        raise BailianError("not_found","试穿记录不存在")
    client = bailian_client()
    task_id = job["remote_task_id"]
    if not task_id:
        top_path = row["dress_path"] or row["upper_path"]
        bottom_path = None if row["dress_path"] else row["lower_path"]
        if not top_path and not bottom_path:
            raise BailianError("garment_missing","所选衣物已经不存在")
        set_job(job_id,stage="uploading",progress=18)
        task_id,request_id = paid_call(job_id,f"tryon_{row['quality']}","tryon",client,client.submit_tryon,
            DATA_DIR / row["person_path"],DATA_DIR / top_path if top_path else None,
            DATA_DIR / bottom_path if bottom_path else None,row["quality"],async_task=True)
        logger.info("job=%s remote_task=%s remote_request=%s",job_id,task_id,request_id or "-")
    set_job(job_id,stage="generating",progress=45)
    result_url = client.wait_tryon(task_id)
    set_job(job_id,stage="downloading_result",progress=88)
    destination = DATA_DIR / "tryons" / f"{tryon_id}-result.png"
    client.download(result_url,destination)
    try:
        with Image.open(destination) as image:
            image.verify()
    except (OSError,UnidentifiedImageError) as exc:
        destination.unlink(missing_ok=True)
        raise BailianError("invalid_result","试穿结果图片暂时无法读取，请继续查询原任务") from exc
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE tryons SET status='succeeded',result_path=? WHERE id=?",(rel_path(destination),tryon_id))
        usage = connection.execute("SELECT id,status,usage_json FROM model_usage WHERE job_id=? AND step='tryon'",(job_id,)).fetchone()
        if usage and usage["status"] != "succeeded":
            details = json.loads(usage["usage_json"])
            details["reported_usage"] = getattr(client,"last_usage",{"availability":"not_reported"})
            usage_service().settle(usage["id"],"succeeded",usage=details,connection=connection)


def quota_json(owner_id: str) -> dict:
    with db() as connection:
        row = connection.execute(
            "SELECT basic_tryons, spent_cents FROM demo_usage WHERE owner_id=?", (owner_id,)
        ).fetchone()
    used = row["basic_tryons"] if row else 0
    spent = usage_service(owner_id).summary(owner_id)["committed_cents"]
    return {
        "remaining_tryons": max(0, VISITOR_TRYON_LIMIT - used),
        "remaining_cents": max(0, VISITOR_BUDGET_CENTS - spent),
    }


def reserve_visitor_tryon(connection: sqlite3.Connection, owner_id: str) -> bool:
    row = connection.execute(
        "SELECT basic_tryons, spent_cents FROM demo_usage WHERE owner_id=?", (owner_id,)
    ).fetchone()
    used = row["basic_tryons"] if row else 0
    spent = connection.execute("SELECT COALESCE(SUM(estimated_cents),0) FROM model_usage WHERE owner_id=? AND amount_state<>'released'", (owner_id,)).fetchone()[0]
    global_spent = connection.execute("SELECT COALESCE(SUM(estimated_cents),0) FROM model_usage WHERE amount_state<>'released'").fetchone()[0]
    if (
        used >= VISITOR_TRYON_LIMIT
        or spent + BASIC_TRYON_CENTS > VISITOR_BUDGET_CENTS
        or global_spent + BASIC_TRYON_CENTS > GLOBAL_BUDGET_CENTS
    ):
        return False
    connection.execute(
        """INSERT INTO demo_usage(owner_id, basic_tryons, spent_cents) VALUES (?, 1, ?)
           ON CONFLICT(owner_id) DO UPDATE SET
             basic_tryons=basic_tryons + 1, spent_cents=spent_cents + excluded.spent_cents""",
        (owner_id, BASIC_TRYON_CENTS),
    )
    return True


def owns_file(filename: str) -> bool:
    return wardrobe_service().owns_file(current_user(), filename)


@app.before_request
def require_demo_login():
    if not AUTH_ENABLED or request.endpoint in {"index", "static", "health", "auth_status", "auth_login"}:
        return None
    if current_owner_id():
        return None
    if request.path.startswith("/api/"):
        return api_error("请输入演示访问密码", 401, "auth_required")
    abort(401)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/auth/status")
def auth_status():
    authenticated = not AUTH_ENABLED or bool(current_owner_id())
    role = current_role() if authenticated else None
    return jsonify({
        "required": AUTH_ENABLED,
        "authenticated": authenticated,
        "role": role,
        "owner_key": current_owner_id() if authenticated else None,
        "quota": quota_json(current_owner_id()) if authenticated and role == "visitor" else None,
    })


@app.post("/api/auth/login")
def auth_login():
    if not AUTH_ENABLED:
        return jsonify({"authenticated": True, "role": "admin"})
    if not SESSION_SECRET:
        return api_error("服务器尚未配置 FITMIX_SECRET_KEY", 503, "auth_not_configured")
    password = str((request.get_json(silent=True) or {}).get("password") or "")
    previous_owner = session.get("owner_id") if session.get("role") == "visitor" else None
    if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
        role, owner_id = "admin", "admin"
    elif hmac.compare_digest(password, ACCESS_PASSWORD):
        role, owner_id = "visitor", previous_owner or f"visitor:{uuid.uuid4().hex}"
    else:
        return api_error("访问密码不正确", 401, "invalid_password")
    session.clear()
    session.permanent = True
    session.update(role=role, owner_id=owner_id)
    return jsonify({
        "authenticated": True,
        "role": role,
        "quota": quota_json(owner_id) if role == "visitor" else None,
    })


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return "", 204


@app.get("/files/<path:filename>")
def stored_file(filename: str):
    if not owns_file(filename):
        abort(404)
    return send_from_directory(DATA_DIR, filename)


@app.get("/thumbnails/<int:size>/<path:filename>")
def thumbnail_file(size: int, filename: str):
    size = max(64, min(size, 960))
    if not owns_file(filename):
        abort(404)
    source = (DATA_DIR / filename).resolve()
    if not source.is_relative_to(DATA_DIR.resolve()) or not source.is_file():
        abort(404)
    cache_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, f'{filename}:{source.stat().st_mtime_ns}:{size}')}.webp"
    cache = DATA_DIR / "thumbnails" / cache_name
    if not cache.exists():
        with Image.open(source) as image:
            preview = ImageOps.exif_transpose(image).copy()
            preview.thumbnail((size, size), Image.Resampling.LANCZOS)
            preview.save(cache, "WEBP", quality=82, method=4)
    return send_from_directory(DATA_DIR / "thumbnails", cache_name, max_age=31536000)


@app.get("/api/health")
def health():
    client = bailian_client()
    with db() as connection:
        connection.execute("SELECT 1").fetchone()
    return jsonify({
        "status": "ok",
        "app_name": "VestiAI",
        "pid": os.getpid(),
        "server_instance": os.environ.get("FITMIX_SERVER_INSTANCE", ""),
        "credentials_configured": client.configured,
        "desktop_mode": DESKTOP_MODE,
        "auth_enabled": AUTH_ENABLED,
    })


@app.get("/api/settings")
def get_settings():
    if not DESKTOP_MODE:
        abort(404)
    values = env_values()
    return jsonify(
        {
            "workspace_id": values.get("DASHSCOPE_WORKSPACE_ID", ""),
            "api_key_configured": bool(values.get("DASHSCOPE_API_KEY")),
        }
    )


@app.post("/api/settings")
def update_settings():
    if not DESKTOP_MODE:
        abort(404)
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return api_error("设置请求来源无效", 403, "invalid_origin")
    if not request.is_json:
        return api_error("设置请求必须使用 JSON", 415, "invalid_content_type")
    body = request.get_json(silent=True) or {}
    current = env_values()
    workspace_id = str(body.get("workspace_id") or "").strip()
    api_key = str(body.get("api_key") or "").strip() or current.get("DASHSCOPE_API_KEY", "")
    if not workspace_id or any(character.isspace() for character in workspace_id):
        return api_error("Workspace ID 不能为空且不能包含空格")
    if not api_key or any(character.isspace() for character in api_key):
        return api_error("API Key 不能为空且不能包含空格")
    save_desktop_credentials(workspace_id, api_key)
    return jsonify({"credentials_configured": True})


@app.get("/api/person-images")
def list_person_images():
    return jsonify({"items": wardrobe_service().person_images(current_user()), "limit": MAX_PERSON_IMAGES})


@app.post("/api/person-images")
def create_person_image():
    owner_id = current_owner_id()
    try:
        stored_path = save_upload(request.files.get("image"), "person-images")
    except ValueError as exc:
        return api_error(str(exc))
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT name FROM person_images WHERE owner_id=?", (owner_id,)).fetchall()
        if len(rows) >= MAX_PERSON_IMAGES:
            (DATA_DIR / stored_path).unlink(missing_ok=True)
            return api_error("最多保存 6 张形象照，请先删除一张再添加", 409, "limit_reached")
        used = {row["name"] for row in rows}
        slot = next(number for number in range(1, MAX_PERSON_IMAGES + 1) if f"形象{number}" not in used)
        image_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO person_images(id, owner_id, name, path) VALUES (?, ?, ?, ?)",
            (image_id, owner_id, f"形象{slot}", stored_path),
        )
        row = connection.execute(
            "SELECT * FROM person_images WHERE id=? AND owner_id=?", (image_id, owner_id)
        ).fetchone()
    return jsonify(person_image_json(row)), 201


@app.delete("/api/person-images/<image_id>")
def delete_person_image(image_id: str):
    owner_id = current_owner_id()
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM person_images WHERE id=? AND owner_id=?", (image_id, owner_id)
        ).fetchone()
        if not row:
            return api_error("形象照不存在", 404, "not_found")
        connection.execute("DELETE FROM person_images WHERE id=? AND owner_id=?", (image_id, owner_id))
    (DATA_DIR / row["path"]).unlink(missing_ok=True)
    return "", 204


@app.post("/api/looks")
def create_look_route():
    owner_id = current_owner_id()
    source_kind = request.form.get("source_kind", "photo")
    if source_kind not in {"photo", "composition"}:
        return api_error("source_kind 只能是 photo 或 composition")
    try:
        source_path = save_upload(request.files.get("image"), "originals")
    except ValueError as exc:
        return api_error(str(exc))
    look_id = uuid.uuid4().hex
    with db() as connection:
        connection.execute(
            "INSERT INTO looks(id, owner_id, source_path, source_kind) VALUES (?, ?, ?, ?)",
            (look_id, owner_id, source_path, source_kind),
        )
    try:
        job_id = create_job("look", look_id, owner_id=owner_id)
    except Exception:
        with db() as connection:
            connection.execute("DELETE FROM looks WHERE id=?", (look_id,))
        (DATA_DIR / source_path).unlink(missing_ok=True)
        raise
    enqueue(job_id, "look", look_id, process_look, look_id)
    return jsonify({"look_id": look_id, "job_id": job_id}), 202


@app.post("/api/tryon-drafts")
def create_tryon_draft():
    owner_id = current_owner_id()
    try:
        source_path = save_upload(request.files.get("image"), "originals")
    except ValueError as exc:
        return api_error(str(exc))
    look_id = uuid.uuid4().hex
    with db() as connection:
        connection.execute(
            "INSERT INTO looks(id, owner_id, source_path, source_kind, draft) VALUES (?, ?, ?, 'composition', 1)",
            (look_id, owner_id, source_path),
        )
    try:
        job_id = create_job("tryon_draft", look_id, owner_id=owner_id)
    except Exception:
        with db() as connection:
            connection.execute("DELETE FROM looks WHERE id=?", (look_id,))
        (DATA_DIR / source_path).unlink(missing_ok=True)
        raise
    enqueue(job_id, "tryon_draft", look_id, process_look, look_id)
    return jsonify({"draft_id": look_id, "job_id": job_id}), 202


@app.get("/api/tryon-drafts/<look_id>")
def get_tryon_draft(look_id: str):
    return jsonify(wardrobe_service().look(current_user(), look_id, draft=True))


@app.post("/api/tryon-drafts/<look_id>/save")
def save_tryon_draft(look_id: str):
    try:
        _, item = materialize_draft_look(look_id, current_owner_id(), publish_look=True)
    except ValueError as exc:
        return api_error(str(exc), 409, "draft_not_ready")
    if not item:
        return api_error("临时搭配不存在或已经保存", 404, "not_found")
    return jsonify(item), 201


@app.post("/api/looks/from-garments")
def create_look_from_garments():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return api_error("请提供 JSON 对象和 garment_ids 数组")
    garment_ids = body.get("garment_ids") or []
    return jsonify(wardrobe_service().save_outfit(current_user(), garment_ids)), 201


@app.get("/api/usage")
def get_usage():
    service = usage_service()
    result = service.summary(current_owner_id())
    result.update(operations=dict(service.policy.estimates),remaining_cents=result["user_remaining_cents"])
    return jsonify(result)


@app.get("/api/jobs")
def list_jobs():
    with db() as connection:
        rows = connection.execute("SELECT id FROM jobs WHERE owner_id=? AND status IN ('queued','running','unknown','waiting_result') ORDER BY created_at DESC",(current_owner_id(),)).fetchall()
    return jsonify({"items":[wardrobe_service().job(current_user(),row["id"]) for row in rows]})


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id: str):
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?",(job_id,current_owner_id())).fetchone()
        if not row:
            return api_error("任务不存在",404,"not_found")
        if row["status"] == "succeeded" or row["status"] == "running" and _alive(row["worker_pid"]):
            return jsonify({"job_id":job_id,"entity_id":row["entity_id"],"kind":row["kind"]}),202
        if row["remote_task_id"]:
            permitted = row["kind"] == "tryon" and row["status"] != "failed"
        else:
            steps = connection.execute("SELECT status FROM model_usage WHERE job_id=?",(job_id,)).fetchall()
            permitted = row["status"] != "unknown" and all(step["status"] in {"reserved","succeeded"} for step in steps)
            if row["status"] == "failed":
                permitted = permitted and bool(steps) and all(step["status"] == "succeeded" for step in steps)
        if not permitted:
            return api_error("原提交结果需要核实，不能重新发送收费请求",409,"not_resumable")
        connection.execute("UPDATE jobs SET status='queued',worker_pid=NULL,worker_token=NULL,error_code=NULL,error_message=NULL WHERE id=?",(job_id,))
        connection.execute("UPDATE tryons SET status='queued' WHERE id=? AND ?='tryon'",(row["entity_id"],row["kind"]))
    _dispatch(row)
    return jsonify({"job_id":job_id,"entity_id":row["entity_id"],"kind":row["kind"]}),202


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    return jsonify(wardrobe_service().job(current_user(), job_id))


@app.get("/api/looks")
def list_looks():
    return jsonify({"items": wardrobe_service().looks(current_user())})


@app.get("/api/looks/<look_id>")
def get_look(look_id: str):
    return jsonify(wardrobe_service().look(current_user(), look_id))


@app.patch("/api/looks/<look_id>")
def update_look(look_id: str):
    owner_id = current_owner_id()
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("favorite"), bool):
        return api_error("favorite 必须是布尔值")
    with db() as connection:
        cursor = connection.execute(
            "UPDATE looks SET favorite=? WHERE id=? AND owner_id=? AND draft=0",
            (int(body["favorite"]), look_id, owner_id),
        )
        row = connection.execute(
            "SELECT * FROM looks WHERE id=? AND owner_id=? AND draft=0", (look_id, owner_id)
        ).fetchone()
        item = look_json(connection, row) if row else None
    if not cursor.rowcount:
        return api_error("穿搭不存在", 404, "not_found")
    return jsonify(item)


@app.get("/api/garments")
def list_garments():
    return jsonify({"items": wardrobe_service().search(current_user())})


@app.post("/api/garments")
def create_garment_route():
    owner_id = current_owner_id()
    try:
        source_path = save_upload(request.files.get("image"), "garment-sources")
    except ValueError as exc:
        return api_error(str(exc))
    garment_id = uuid.uuid4().hex
    try:
        job_id = create_job("garment", garment_id, source_path, owner_id)
    except Exception:
        (DATA_DIR / source_path).unlink(missing_ok=True)
        raise
    enqueue(job_id, "garment", garment_id, process_direct_garment, garment_id, source_path, None, owner_id)
    return jsonify({"garment_id": garment_id, "job_id": job_id}), 202


@app.get("/api/garments/<garment_id>")
def get_garment(garment_id: str):
    return jsonify(wardrobe_service().garment(current_user(), garment_id))


@app.post("/api/jobs/<job_id>/retry-garment")
def retry_failed_garment(job_id: str):
    owner_id = current_owner_id()
    body = request.get_json(silent=True) or {}
    garment_type = body.get("type")
    if garment_type not in VALID_TYPES:
        return api_error("请选择上衣、下装或套装/连衣裙")
    with db() as connection:
        old_job = connection.execute(
            "SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, owner_id)
        ).fetchone()
    if not old_job or old_job["kind"] != "garment" or old_job["status"] != "failed" or not old_job["input_path"]:
        return api_error("这个任务不能手动重试", 409, "not_retryable")
    if not (DATA_DIR / old_job["input_path"]).exists():
        return api_error("原始图片已经不存在，请重新添加", 409, "source_missing")
    new_job_id = create_job("garment", old_job["entity_id"], old_job["input_path"], owner_id, payload={"forced_type": garment_type})
    enqueue(
        new_job_id,
        "garment",
        old_job["entity_id"],
        process_direct_garment,
        old_job["entity_id"],
        old_job["input_path"],
        garment_type,
        owner_id,
    )
    return jsonify({"garment_id": old_job["entity_id"], "job_id": new_job_id}), 202


@app.post("/api/looks/<look_id>/parse")
def retry_parse(look_id: str):
    owner_id = current_owner_id()
    body = request.get_json(silent=True) or {}
    garment_types = body.get("types") or []
    if not isinstance(garment_types, list) or not garment_types or any(item not in VALID_TYPES for item in garment_types):
        return api_error("请选择上衣、下装或套装/连衣裙类型")
    garment_types = list(dict.fromkeys(garment_types))
    if "dress" in garment_types and len(garment_types) > 1:
        return api_error("套装/连衣裙不能与上衣或下装同时分割")
    with db() as connection:
        exists = connection.execute(
            "SELECT 1 FROM looks WHERE id=? AND owner_id=?", (look_id, owner_id)
        ).fetchone()
    if not exists:
        return api_error("穿搭不存在", 404, "not_found")
    job_id = create_job("parse", look_id, owner_id=owner_id, payload={"types": garment_types})
    enqueue(job_id, "parse", look_id, process_manual_parse, look_id, garment_types)
    return jsonify({"look_id": look_id, "job_id": job_id}), 202


@app.patch("/api/garments/<garment_id>")
def update_garment(garment_id: str):
    owner_id = current_owner_id()
    body = request.get_json(silent=True) or {}
    allowed = {"favorite", "label_zh", "description_zh", "type", "seasons"}
    values = {key: value for key, value in body.items() if key in allowed}
    if not values:
        return api_error("没有可更新的字段")
    if "favorite" in values and not isinstance(values["favorite"], bool):
        return api_error("favorite 必须是布尔值")
    if "type" in values and values["type"] not in VALID_TYPES:
        return api_error("服装类型不正确")
    if "seasons" in values and (
        not isinstance(values["seasons"], list)
        or any(item not in {"spring", "summer", "autumn", "winter"} for item in values["seasons"])
    ):
        return api_error("季节只能选择春秋、夏或冬")
    if "label_zh" in values:
        values["label_zh"] = str(values["label_zh"]).strip()[:6]
        if not values["label_zh"]:
            return api_error("单品名称不能为空")
    if "description_zh" in values:
        values["description_zh"] = str(values["description_zh"]).strip()[:240]
    if "favorite" in values:
        values["favorite"] = int(values["favorite"])
    if "seasons" in values:
        values["seasons"] = json.dumps(list(dict.fromkeys(values["seasons"])), ensure_ascii=False)
    with db() as connection:
        existing = connection.execute(
            "SELECT * FROM garments WHERE id=? AND owner_id=?", (garment_id, owner_id)
        ).fetchone()
        if not existing:
            return api_error("衣物不存在", 404, "not_found")
        if "label_zh" in values:
            values["label_zh"] = unique_garment_name(
                connection,
                values.get("description_zh", existing["description_zh"]),
                values.get("type", existing["type"]),
                owner_id,
                preferred=values["label_zh"],
                exclude_id=garment_id,
            )
        elif "description_zh" in values:
            values["label_zh"] = unique_garment_name(
                connection,
                values["description_zh"],
                values.get("type", existing["type"]),
                owner_id,
                exclude_id=garment_id,
            )
        assignments = ", ".join(f"{key}=?" for key in values)
        connection.execute(
            f"UPDATE garments SET {assignments} WHERE id=? AND owner_id=?",
            (*values.values(), garment_id, owner_id),
        )
        row = connection.execute(
            "SELECT * FROM garments WHERE id=? AND owner_id=?", (garment_id, owner_id)
        ).fetchone()
    return jsonify(garment_json(row))


@app.post("/api/garments/<garment_id>/parse")
def reparse_garment(garment_id: str):
    owner_id = current_owner_id()
    body = request.get_json(silent=True) or {}
    garment_type = body.get("type")
    if garment_type not in VALID_TYPES:
        return api_error("请选择正确的服装类型")
    with db() as connection:
        exists = connection.execute(
            "SELECT 1 FROM garments WHERE id=? AND owner_id=?", (garment_id, owner_id)
        ).fetchone()
    if not exists:
        return api_error("衣物不存在", 404, "not_found")
    job_id = create_job("garment_parse", garment_id, owner_id=owner_id, payload={"type": garment_type})
    enqueue(job_id, "garment_parse", garment_id, process_reparse_garment, garment_id, garment_type)
    return jsonify({"garment_id": garment_id, "job_id": job_id}), 202


@app.post("/api/garments/<garment_id>/merge")
def merge_garment(garment_id: str):
    owner_id = current_owner_id()
    target_id = (request.get_json(silent=True) or {}).get("target_id")
    if not target_id or target_id == garment_id:
        return api_error("请选择另一件保留单品")
    with db() as connection:
        source = connection.execute(
            "SELECT * FROM garments WHERE id=? AND owner_id=?", (garment_id, owner_id)
        ).fetchone()
        target = connection.execute(
            "SELECT * FROM garments WHERE id=? AND owner_id=?", (target_id, owner_id)
        ).fetchone()
        if not source or not target:
            return api_error("待合并单品不存在", 404, "not_found")
        if source["type"] != target["type"]:
            return api_error("不同类型的单品不能合并")
        connection.execute(
            """INSERT OR IGNORE INTO look_garments(look_id, garment_id)
               SELECT look_id, ? FROM look_garments WHERE garment_id=?""",
            (target_id, garment_id),
        )
        for column in ("upper_id", "lower_id", "dress_id"):
            connection.execute(
                f"UPDATE tryons SET {column}=? WHERE {column}=? AND owner_id=?",
                (target_id, garment_id, owner_id),
            )
        connection.execute("DELETE FROM jobs WHERE entity_id=? AND owner_id=?", (garment_id, owner_id))
        connection.execute("DELETE FROM garments WHERE id=? AND owner_id=?", (garment_id, owner_id))
        source_still_used = source["source_path"] and connection.execute(
            "SELECT 1 FROM garments WHERE owner_id=? AND source_path=? LIMIT 1",
            (owner_id, source["source_path"]),
        ).fetchone()
    paths = {stored for stored in (source["source_path"], source["crop_path"], source["mask_path"]) if stored}
    if source_still_used:
        paths.discard(source["source_path"])
    for stored in paths:
        (DATA_DIR / stored).unlink(missing_ok=True)
    return jsonify({"merged_id": garment_id, "kept_id": target_id})


@app.post("/api/tryon-authorizations")
def authorize_tryon_route():
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return api_error("请从本应用确认生成",403,"invalid_origin")
    uploaded_person = None
    try:
        upload = request.files.get("person")
        if upload and upload.filename:
            uploaded_person = save_upload(upload,"tryons")
        prepared = tryon_service().authorize(
            current_user(),{kind:request.form.get(f"{kind}_id") or None for kind in ("upper","lower","dress")},
            quality=request.form.get("quality","basic"),person_image_id=request.form.get("person_image_id") or None,
            source_tryon_id=request.form.get("source_tryon_id") or None,uploaded_person_path=uploaded_person,
            draft_look_id=request.form.get("draft_look_id") or None,action=request.form.get("action","new"))
        return jsonify(prepared),201
    except Exception as exc:
        if uploaded_person:
            (DATA_DIR/uploaded_person).unlink(missing_ok=True)
        if isinstance(exc,ValueError) and not isinstance(exc,ServiceError):
            return api_error(str(exc))
        raise


@app.post("/api/tryon-authorizations/<authorization_id>/confirm")
def confirm_tryon_route(authorization_id: str):
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return api_error("请从本应用确认生成",403,"invalid_origin")
    body = request.get_json(silent=True)
    if not isinstance(body,dict) or set(body) != {"request_id"}:
        return api_error("请确认已展示的本次生成范围")
    return jsonify(tryon_service().confirm(current_user(),authorization_id,body["request_id"]))


@app.post("/api/tryons")
def create_tryon_route():
    body = request.get_json(silent=True)
    if not isinstance(body,dict) or not body.get("authorization_id") or not body.get("request_id"):
        return api_error("请先确认本次试穿和费用",403,"authorization_required")
    if set(body) != {"authorization_id","request_id"}:
        return api_error("生成范围已改变，请重新确认",409,"authorization_mismatch")
    return jsonify(tryon_service().create(current_user(),**body)),202


@app.post("/api/tryons/<tryon_id>/retry")
def retry_tryon_plus(tryon_id: str):
    if current_role() == "visitor":
        return api_error("高质量试穿仅管理员可用",403,"admin_only")
    return api_error("请先确认高质量试穿和本次费用",403,"authorization_required")


@app.get("/api/tryons")
def list_tryons():
    return jsonify({"items": tryon_service().history(current_user())})


@app.post("/api/tryons/<tryon_id>/save-look")
def save_tryon_as_look(tryon_id: str):
    owner_id = current_owner_id()
    with db() as connection:
        existing = connection.execute(
            "SELECT * FROM looks WHERE source_tryon_id=? AND owner_id=?", (tryon_id, owner_id)
        ).fetchone()
        if existing:
            return jsonify(look_json(connection, existing))
        tryon = connection.execute(
            "SELECT * FROM tryons WHERE id=? AND owner_id=?", (tryon_id, owner_id)
        ).fetchone()
    if not tryon or tryon["status"] != "succeeded" or not tryon["result_path"]:
        return api_error("只有已完成的试穿结果才能保存为穿搭")
    source = DATA_DIR / tryon["result_path"]
    if not source.exists():
        return api_error("试穿结果图片已经不存在", 409, "source_missing")
    if tryon["draft_look_id"]:
        try:
            materialize_draft_look(tryon["draft_look_id"], owner_id, publish_look=False)
        except ValueError as exc:
            return api_error(str(exc), 409, "draft_not_ready")
        with db() as connection:
            tryon = connection.execute(
                "SELECT * FROM tryons WHERE id=? AND owner_id=?", (tryon_id, owner_id)
            ).fetchone()
    look_id = uuid.uuid4().hex
    destination = DATA_DIR / "originals" / f"{uuid.uuid4().hex}{source.suffix or '.png'}"
    shutil.copy2(source, destination)
    garment_ids = [item for item in (tryon["upper_id"], tryon["lower_id"], tryon["dress_id"]) if item]
    with db() as connection:
        season_rows = connection.execute(
            f"SELECT seasons FROM garments WHERE owner_id=? AND id IN ({','.join('?' for _ in garment_ids)})",
            (owner_id, *garment_ids),
        ).fetchall() if garment_ids else []
        seasons = list(dict.fromkeys(season for row in season_rows for season in json.loads(row["seasons"] or "[]")))
        connection.execute(
            """INSERT INTO looks(
                   id, owner_id, source_path, source_kind, source_tryon_id, title_en, style_zh, seasons, palette)
               VALUES (?, ?, ?, 'tryon', ?, 'AI Try-On Look', 'AI 试穿搭配', ?, '[]')""",
            (look_id, owner_id, rel_path(destination), tryon_id, json.dumps(seasons, ensure_ascii=False)),
        )
        for garment_id in garment_ids:
            connection.execute(
                "INSERT OR IGNORE INTO look_garments(look_id, garment_id) VALUES (?, ?)",
                (look_id, garment_id),
            )
        row = connection.execute("SELECT * FROM looks WHERE id=?", (look_id,)).fetchone()
        result = look_json(connection, row)
    return jsonify(result), 201


@app.delete("/api/looks/<look_id>")
def delete_look(look_id: str):
    owner_id = current_owner_id()
    with db() as connection:
        look = connection.execute(
            "SELECT source_path FROM looks WHERE id=? AND owner_id=?", (look_id, owner_id)
        ).fetchone()
        if not look:
            return api_error("穿搭不存在", 404, "not_found")
        source_garments = connection.execute(
            "SELECT id, source_path FROM garments WHERE look_id=? AND owner_id=?", (look_id, owner_id)
        ).fetchall()
        for garment in source_garments:
            if garment["source_path"]:
                continue
            original = DATA_DIR / look["source_path"]
            destination = DATA_DIR / "garment-sources" / f"{uuid.uuid4().hex}{original.suffix or '.png'}"
            shutil.copy2(original, destination)
            connection.execute(
                "UPDATE garments SET source_path=? WHERE id=?", (rel_path(destination), garment["id"])
            )
        connection.execute("DELETE FROM jobs WHERE entity_id=? AND owner_id=?", (look_id, owner_id))
        connection.execute("DELETE FROM looks WHERE id=? AND owner_id=?", (look_id, owner_id))
    (DATA_DIR / look["source_path"]).unlink(missing_ok=True)
    return "", 204


@app.delete("/api/garments/<garment_id>")
def delete_garment(garment_id: str):
    owner_id = current_owner_id()
    with db() as connection:
        garment = connection.execute(
            "SELECT * FROM garments WHERE id=? AND owner_id=?", (garment_id, owner_id)
        ).fetchone()
        if not garment:
            return api_error("衣物不存在", 404, "not_found")
        connection.execute("DELETE FROM jobs WHERE entity_id=? AND owner_id=?", (garment_id, owner_id))
        connection.execute("DELETE FROM garments WHERE id=? AND owner_id=?", (garment_id, owner_id))
        source_still_used = garment["source_path"] and connection.execute(
            "SELECT 1 FROM garments WHERE owner_id=? AND source_path=? LIMIT 1",
            (owner_id, garment["source_path"]),
        ).fetchone()
    paths = {path for path in (garment["source_path"], garment["crop_path"], garment["mask_path"]) if path}
    if source_still_used:
        paths.discard(garment["source_path"])
    for stored in paths:
        (DATA_DIR / stored).unlink(missing_ok=True)
    return "", 204


@app.errorhandler(ServiceError)
def service_error(error):
    return api_error(str(error), error.status, error.code)


@app.errorhandler(413)
def too_large(_error):
    return api_error("上传内容超过 20MB", 413, "too_large")


init_db()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("FITMIX_HTTP_PORT", "5050")), debug=False, threaded=True, use_reloader=False)
