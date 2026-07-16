"""Listing 侧 Task 状态机 + 本地持久化（R112-RECOVERY-GATE-D2）。

PRD v2.5 CTRL-01~04 - 真实调度状态机。
PCE 的 /api/v1/tasks/{id}/status 和 /checkpoint 子端点未暴露（404），
Listing 侧自行维护状态机 + checkpoint + receipt，持久化到本地 SQLite。

状态流转：pending -> running -> completed/failed
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("task-state")

_DB_PATH = os.environ.get(
    "LISTING_TASK_DB",
    str(Path(__file__).parent / "task_states.db"),
)

_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """初始化本地 task 状态表（幂等）。"""
    with _lock, _get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_states (
                task_id        TEXT PRIMARY KEY,
                tenant_id      TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                payload        TEXT,
                result         TEXT,
                error          TEXT,
                created_at     REAL NOT NULL,
                updated_at     REAL NOT NULL,
                completed_at   REAL
            );
            CREATE TABLE IF NOT EXISTS task_checkpoints (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id        TEXT NOT NULL,
                phase          TEXT NOT NULL,
                detail         TEXT,
                created_at     REAL NOT NULL,
                FOREIGN KEY (task_id) REFERENCES task_states(task_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cp_task ON task_checkpoints(task_id);
            CREATE INDEX IF NOT EXISTS idx_state_tenant ON task_states(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_state_status ON task_states(status);
            """
        )
        conn.commit()
    logger.info(f"[task-state] DB initialized at {_DB_PATH}")


def create_local_task(
    task_id: str,
    tenant_id: str,
    payload: dict | None = None,
) -> None:
    """创建本地 task 记录（status=pending）。"""
    now = time.time()
    with _lock, _get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO task_states "
            "(task_id, tenant_id, status, payload, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?, ?)",
            (task_id, tenant_id, json.dumps(payload or {}), now, now),
        )
        conn.commit()
    logger.info(f"[task-state] Created local task {task_id} (tenant={tenant_id})")


def set_status(
    task_id: str,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> bool:
    """更新 task 状态。status: pending/running/completed/failed。

    Returns:
        bool: 成功 True，task 不存在 False
    """
    valid = {"pending", "running", "completed", "failed"}
    if status not in valid:
        logger.error(f"[task-state] Invalid status '{status}', must be one of {valid}")
        return False

    now = time.time()
    completed_at = now if status in ("completed", "failed") else None

    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now]

    if result is not None:
        sets.append("result = ?")
        params.append(json.dumps(result))
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if completed_at is not None:
        sets.append("completed_at = ?")
        params.append(completed_at)

    params.append(task_id)

    with _lock, _get_conn() as conn:
        cur = conn.execute(
            f"UPDATE task_states SET {', '.join(sets)} WHERE task_id = ?",
            params,
        )
        conn.commit()
        ok = cur.rowcount > 0

    if ok:
        logger.info(f"[task-state] {task_id} -> {status}")
    else:
        logger.warning(f"[task-state] task not found: {task_id}")
    return ok


def add_checkpoint(
    task_id: str,
    phase: str,
    detail: dict | None = None,
) -> None:
    """写入 checkpoint。"""
    now = time.time()
    with _lock, _get_conn() as conn:
        conn.execute(
            "INSERT INTO task_checkpoints (task_id, phase, detail, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, phase, json.dumps(detail or {}), now),
        )
        conn.commit()
    logger.info(f"[task-state] Checkpoint {task_id} @ {phase}")


def get_task(task_id: str, tenant_id: str | None = None) -> dict | None:
    """查询 task 状态。如果提供 tenant_id，做租户校验。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM task_states WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    if not row:
        return None

    if tenant_id and row["tenant_id"] != tenant_id:
        return None  # 租户不匹配，视为不存在

    return {
        "task_id": row["task_id"],
        "tenant_id": row["tenant_id"],
        "status": row["status"],
        "payload": json.loads(row["payload"] or "{}"),
        "result": json.loads(row["result"] or "null"),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def get_checkpoints(task_id: str) -> list[dict]:
    """查询 task 的所有 checkpoint。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
    return [
        {
            "phase": r["phase"],
            "detail": json.loads(r["detail"] or "{}"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_tasks(
    tenant_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """列出 tasks（可按 tenant/status 过滤）。"""
    query = "SELECT * FROM task_states"
    conditions: list[str] = []
    params: list[Any] = []

    if tenant_id:
        conditions.append("tenant_id = ?")
        params.append(tenant_id)
    if status:
        conditions.append("status = ?")
        params.append(status)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        {
            "task_id": r["task_id"],
            "tenant_id": r["tenant_id"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "completed_at": r["completed_at"],
        }
        for r in rows
    ]


def gen_task_id() -> str:
    """生成唯一 task_id。"""
    return f"lst-{uuid.uuid4().hex[:16]}-{int(time.time())}"
