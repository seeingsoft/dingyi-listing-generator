"""Listing Task Status / Checkpoint / Receipt HTTP 端点（R112-RECOVERY-GATE-D2）。

PRD v2.5 CTRL-01~04 - 状态机端点。
提供 Listing 侧 task 查询、status 更新、checkpoint 查询、receipt 生成。
"""

import logging
import time

from flask import Blueprint, jsonify, request

from task_state import (
    add_checkpoint,
    get_checkpoints,
    get_task,
    get_task_tenant,
    list_tasks,
    set_status,
)

logger = logging.getLogger("status-endpoint")

bp = Blueprint("task_status", __name__, url_prefix="/api/v1/tasks")


def _get_tenant_id() -> str | None:
    """从请求头提取 tenant_id（请求级，非进程级）。"""
    tid = request.headers.get("X-Tenant-ID")
    if not tid:
        return None
    return tid.strip()


def _check_tenant_access(task_id: str, tenant_id: str) -> tuple[dict | None, tuple | None]:
    """GATE-H L5: 检查 tenant 访问权限。

    Returns:
        (task, None) - 有权限，返回 task dict
        (None, (404_response)) - task 不存在
        (None, (403_response)) - task 存在但 tenant 不匹配
    """
    # 先查 task 是否存在（不做 tenant 校验）
    actual_tenant = get_task_tenant(task_id)
    if actual_tenant is None:
        return None, (jsonify({"error": "task not found"}), 404)
    if actual_tenant != tenant_id:
        return None, (jsonify({"error": "forbidden: task belongs to different tenant"}), 403)
    # 有权限，返回完整 task
    return get_task(task_id, tenant_id=tenant_id), None


@bp.route("/<task_id>", methods=["GET"])
def get_task_status(task_id: str):
    """查询 task 状态。需要 X-Tenant-ID header。"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return jsonify({"error": "X-Tenant-ID header required"}), 401

    task, err = _check_tenant_access(task_id, tenant_id)
    if err:
        return err
    return jsonify(task)


@bp.route("/<task_id>/status", methods=["POST"])
def update_status(task_id: str):
    """更新 task 状态。需要 X-Tenant-ID header。"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return jsonify({"error": "X-Tenant-ID header required"}), 401

    task, err = _check_tenant_access(task_id, tenant_id)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    status = body.get("status", "").strip()
    if not status:
        return jsonify({"error": "status required"}), 400

    result = body.get("result")
    error = body.get("error")

    ok = set_status(task_id, status, result=result, error=error)
    if ok:
        return jsonify({"task_id": task_id, "status": status, "ok": True})
    return jsonify({"error": "update failed"}), 500


@bp.route("/<task_id>/checkpoint", methods=["POST"])
def add_cp(task_id: str):
    """写入 checkpoint。需要 X-Tenant-ID header。"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return jsonify({"error": "X-Tenant-ID header required"}), 401

    task, err = _check_tenant_access(task_id, tenant_id)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    phase = body.get("phase", "").strip()
    if not phase:
        return jsonify({"error": "phase required"}), 400

    detail = body.get("detail")
    add_checkpoint(task_id, phase, detail=detail)
    return jsonify({"task_id": task_id, "phase": phase, "ok": True})


@bp.route("/<task_id>/checkpoints", methods=["GET"])
def get_cps(task_id: str):
    """查询 task 的所有 checkpoint。"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return jsonify({"error": "X-Tenant-ID header required"}), 401

    task, err = _check_tenant_access(task_id, tenant_id)
    if err:
        return err

    cps = get_checkpoints(task_id)
    return jsonify({"task_id": task_id, "checkpoints": cps, "total": len(cps)})


@bp.route("/<task_id>/receipt", methods=["GET"])
def get_receipt(task_id: str):
    """生成 task receipt（任务完成后的回执）。"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return jsonify({"error": "X-Tenant-ID header required"}), 401

    task, err = _check_tenant_access(task_id, tenant_id)
    if err:
        return err

    cps = get_checkpoints(task_id)

    receipt = {
        "receipt_id": f"rcpt-{task_id}",
        "task_id": task_id,
        "tenant_id": task["tenant_id"],
        "status": task["status"],
        "created_at": task["created_at"],
        "completed_at": task["completed_at"],
        "duration_seconds": (
            task["completed_at"] - task["created_at"]
            if task["completed_at"]
            else None
        ),
        "result": task["result"],
        "error": task["error"],
        "checkpoints": cps,
        "checkpoint_count": len(cps),
        "generated_at": time.time(),
    }

    return jsonify(receipt)


@bp.route("", methods=["GET"])
def list_all_tasks():
    """列出 tasks（可按 status 过滤）。需要 X-Tenant-ID header。"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return jsonify({"error": "X-Tenant-ID header required"}), 401

    status = request.args.get("status")
    limit = int(request.args.get("limit", "20"))

    tasks = list_tasks(tenant_id=tenant_id, status=status, limit=limit)
    return jsonify({"tasks": tasks, "total": len(tasks), "tenant_id": tenant_id})
