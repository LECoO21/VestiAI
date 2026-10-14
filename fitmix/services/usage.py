"""Atomic model-call budgets. Stored amounts are estimates, never platform bills."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from types import MappingProxyType

from .context import ServiceError


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS model_usage (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    step TEXT NOT NULL,
    model TEXT NOT NULL,
    estimated_cents INTEGER NOT NULL CHECK(estimated_cents >= 0),
    amount_state TEXT NOT NULL CHECK(amount_state IN ('reserved', 'estimated', 'unknown', 'released')),
    status TEXT NOT NULL CHECK(status IN ('reserved', 'submitted', 'unknown', 'succeeded', 'failed', 'released')),
    usage_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, step)
);
CREATE INDEX IF NOT EXISTS model_usage_owner ON model_usage(owner_id);
"""

DEFAULT_MODELS = {
    "recognition": "qwen-vl-plus", "segmentation": "aitryon-parsing-v1",
    "tryon_basic": "aitryon", "tryon_plus": "aitryon-plus",
    "recommendation": None, "agent": None,
}
DEFAULT_ESTIMATES = {
    "recognition": None, "segmentation": None,
    "tryon_basic": 20, "tryon_plus": 50,
    "recommendation": None, "agent": None,
}


@dataclass(frozen=True)
class UsagePolicy:
    """Cumulative limits in RMB cents; None estimates disable that model role."""
    job_limit_cents: int = 100
    user_limit_cents: int = 1000
    global_limit_cents: int = 5000
    estimates: dict | None = None
    models: dict | None = None

    def __post_init__(self):
        for name in ("job_limit_cents", "user_limit_cents", "global_limit_cents"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        estimates = {**DEFAULT_ESTIMATES, **(self.estimates or {})}
        models = {**DEFAULT_MODELS, **(self.models or {})}
        # A changed model cannot inherit a different model's configured price.
        for operation, model in (self.models or {}).items():
            if model != DEFAULT_MODELS.get(operation) and operation not in (self.estimates or {}):
                estimates[operation] = None
        for value in estimates.values():
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
                raise ValueError("Model estimates must be positive integer cents or None")
        for value in models.values():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("Model names must be nonempty strings or None")
        object.__setattr__(self, "estimates", MappingProxyType(estimates))
        object.__setattr__(self, "models", MappingProxyType(models))

    def estimate(self, operation: str) -> int:
        """Return the configured reservation amount, not an actual model bill."""
        amount = self.estimates.get(operation)
        if amount is None or not self.models.get(operation):
            raise ServiceError("该模型尚未配置费用估算，请先在服务端配置每次预算", 409, "estimate_missing")
        return amount


def backfill_legacy_usage(connection, policy: UsagePolicy) -> None:
    """Account for already possible charges during the pre-v2 migration.

    The caller owns the migration transaction. These past calls must be recorded
    even when they exceed today's admission limits. They are estimates with
    unknown billing, and existing visitor aggregate spend is never counted twice.
    """
    if not connection.in_transaction:
        raise ValueError("Legacy backfill requires an active migration transaction")
    cursor = connection.execute("SELECT id,owner_id,quality,status FROM tryons WHERE status<>'queued'")
    columns = [column[0] for column in cursor.description]
    tryons = [dict(zip(columns, row)) for row in cursor.fetchall()]
    assigned = {}
    for tryon in tryons:
        owner = tryon["owner_id"]
        job = connection.execute(
            """SELECT id FROM jobs WHERE kind='tryon' AND entity_id=? AND owner_id=?
               ORDER BY created_at DESC,id DESC LIMIT 1""", (tryon["id"], owner),
        ).fetchone()
        legacy_id = f"legacytryon:{tryon['id']}"
        job_id = job[0] if job else legacy_id
        existing = connection.execute(
            "SELECT estimated_cents,amount_state FROM model_usage WHERE (job_id=? AND step='tryon') OR id=?",
            (job_id, legacy_id),
        ).fetchone()
        if existing:
            if existing[1] != "released":
                assigned[owner] = assigned.get(owner, 0) + existing[0]
            continue
        operation = "tryon_plus" if tryon["quality"] == "plus" else "tryon_basic"
        basis = "legacy_configured_estimate"
        try:
            estimate = policy.estimate(operation)
        except ServiceError as error:
            if error.code != "estimate_missing":
                raise
            estimate = DEFAULT_ESTIMATES[operation]
            basis = "legacy_default_estimate"
        connection.execute(
            """INSERT INTO model_usage(id,job_id,owner_id,operation,step,model,
               estimated_cents,amount_state,status,metadata_json)
               VALUES(?,?,?,?,'tryon',?,?,'unknown','unknown',?)""",
            (legacy_id, job_id, owner, operation, DEFAULT_MODELS[operation], estimate,
             json.dumps({"source": "legacy_tryon", "tryon_id": tryon["id"],
                         "cost_basis": basis}, ensure_ascii=False)),
        )
        assigned[owner] = assigned.get(owner, 0) + estimate
    for owner, spent in connection.execute("SELECT owner_id,spent_cents FROM demo_usage WHERE spent_cents>0").fetchall():
        remainder = max(0, spent - assigned.get(owner, 0))
        if not remainder:
            continue
        identity = f"legacy:{owner}"
        connection.execute(
            """INSERT OR IGNORE INTO model_usage(id,job_id,owner_id,operation,step,model,
               estimated_cents,amount_state,status,metadata_json)
               VALUES(?,?,?,'legacy','legacy','legacy-model-calls',?,'unknown','unknown',?)""",
            (identity, identity, owner, remainder,
             json.dumps({"source": "demo_usage", "cost_basis": "legacy_unallocated_estimate"})),
        )


def _record(cursor, row) -> dict | None:
    if row is None:
        return None
    result = dict(zip((column[0] for column in cursor.description), row))
    result["usage"] = json.loads(result.pop("usage_json"))
    result["metadata"] = json.loads(result.pop("metadata_json"))
    return result


class UsageService:
    """The host supplies a trusted owner; models cannot set budget or ownership."""
    def __init__(self, connection_factory, policy: UsagePolicy | None = None):
        self.db = connection_factory
        self.policy = policy or UsagePolicy()

    def reserve(self, job_id: str, owner_id: str, operation: str, step: str | None = None,
                *, metadata: dict | None = None, connection=None) -> dict:
        """Reserve before sending a request. A supplied transaction stays caller-owned."""
        step = step or operation
        for value in (job_id, owner_id, operation, step):
            if not isinstance(value, str) or not value.strip():
                raise ServiceError("模型调用标识无效")
        if connection is None:
            with self.db() as current:
                current.execute("BEGIN IMMEDIATE")
                return self.reserve(job_id, owner_id, operation, step, metadata=metadata, connection=current)
        if not connection.in_transaction:
            raise ValueError("Budget reservation requires the caller's active transaction")
        cursor = connection.execute("SELECT * FROM model_usage WHERE job_id=? AND step=?", (job_id, step))
        existing = _record(cursor, cursor.fetchone())
        if existing:
            if existing["owner_id"] != owner_id or existing["operation"] != operation:
                raise ServiceError("模型调用记录不属于当前请求", 409, "usage_conflict")
            return existing
        job_owner = connection.execute("SELECT owner_id FROM model_usage WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
        if job_owner and job_owner[0] != owner_id:
            raise ServiceError("模型调用记录不属于当前请求", 409, "usage_conflict")
        estimated_cents = self.policy.estimate(operation)
        model = self.policy.models[operation]
        for scope, sql, parameters, limit in (
            ("job", "job_id=?", (job_id,), self.policy.job_limit_cents),
            ("user", "owner_id=?", (owner_id,), self.policy.user_limit_cents),
            ("global", "1=1", (), self.policy.global_limit_cents),
        ):
            total = connection.execute(
                f"SELECT COALESCE(SUM(estimated_cents),0) FROM model_usage WHERE amount_state<>'released' AND {sql}",
                parameters,
            ).fetchone()[0]
            if total + estimated_cents > limit:
                label = {"job": "本次任务", "user": "当前用户", "global": "应用总"}[scope]
                raise ServiceError(f"{label}模型预算不足", 429, f"{scope}_budget_exhausted")
        usage_id = uuid.uuid4().hex
        details = dict(metadata or {})
        details["cost_basis"] = "configured_estimate"
        connection.execute(
            """INSERT INTO model_usage(id,job_id,owner_id,operation,step,model,estimated_cents,
                      amount_state,status,metadata_json) VALUES(?,?,?,?,?,?,?,'reserved','reserved',?)""",
            (usage_id, job_id, owner_id, operation, step, model, estimated_cents,
             json.dumps(details, ensure_ascii=False)),
        )
        return self.get(usage_id, owner_id, connection=connection)

    def get(self, usage_id: str, owner_id: str, *, connection=None) -> dict:
        if connection is None:
            with self.db() as current:
                return self.get(usage_id, owner_id, connection=current)
        cursor = connection.execute("SELECT * FROM model_usage WHERE id=? AND owner_id=?", (usage_id, owner_id))
        result = _record(cursor, cursor.fetchone())
        if not result:
            raise ServiceError("模型调用记录不存在", 404, "not_found")
        return result

    def settle(self, usage_id: str, state: str, usage: dict | None = None, *,
               owner_id: str | None = None, connection=None) -> dict:
        """Release only a proven unsent call; submitted/uncertain failures retain budget."""
        states = {"submitted": "estimated", "unknown": "unknown", "succeeded": "estimated",
                  "failed": "unknown", "released": "released"}
        if state not in states:
            raise ServiceError("模型调用状态无效")
        if usage is not None and not isinstance(usage, dict):
            raise ServiceError("模型用量必须是对象")
        if connection is None:
            with self.db() as current:
                current.execute("BEGIN IMMEDIATE")
                return self.settle(usage_id, state, usage, owner_id=owner_id, connection=current)
        if not connection.in_transaction:
            raise ValueError("Budget settlement requires the caller's active transaction")
        cursor = connection.execute("SELECT * FROM model_usage WHERE id=?", (usage_id,))
        record = _record(cursor, cursor.fetchone())
        if not record or (owner_id is not None and record["owner_id"] != owner_id):
            raise ServiceError("模型调用记录不存在", 404, "not_found")
        previous = record["status"]
        if previous == state and usage is None:
            return record
        if previous != state and previous in {"succeeded", "failed", "released"}:
            raise ServiceError("模型调用已结束，不能改变结果", 409, "usage_state_conflict")
        if state == "released" and previous not in {"reserved", "released"}:
            raise ServiceError("已经提交或结果待核实的调用不能释放预算", 409, "usage_state_conflict")
        if previous == "unknown" and state == "submitted":
            raise ServiceError("待核实调用只能记录核实结果", 409, "usage_state_conflict")
        connection.execute(
            """UPDATE model_usage SET status=?,amount_state=?,usage_json=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (state, states[state], json.dumps(usage if usage is not None else record["usage"], ensure_ascii=False), usage_id),
        )
        return self.get(usage_id, record["owner_id"], connection=connection)

    def summary(self, owner_id: str) -> dict:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ServiceError("用户身份无效", 401, "auth_required")
        with self.db() as connection:
            rows = connection.execute(
                """SELECT amount_state,COALESCE(SUM(estimated_cents),0) FROM model_usage
                   WHERE owner_id=? GROUP BY amount_state""", (owner_id,),
            ).fetchall()
            amounts = dict(rows)
            global_used = connection.execute(
                "SELECT COALESCE(SUM(estimated_cents),0) FROM model_usage WHERE amount_state<>'released'"
            ).fetchone()[0]
        committed = sum(amounts.get(state, 0) for state in ("reserved", "estimated", "unknown"))
        return {
            "cost_basis": "configured_estimate", "actual_bill_available": False,
            "estimated_cents": amounts.get("estimated", 0),
            "reserved_cents": amounts.get("reserved", 0),
            "unknown_cents": amounts.get("unknown", 0),
            "released_cents": amounts.get("released", 0), "committed_cents": committed,
            "job_limit_cents": self.policy.job_limit_cents,
            "user_limit_cents": self.policy.user_limit_cents,
            "global_limit_cents": self.policy.global_limit_cents,
            "user_remaining_cents": max(0, self.policy.user_limit_cents - committed),
            "global_remaining_cents": max(0, self.policy.global_limit_cents - global_used),
        }
