"""PCE Task 状态机客户端 — 对接 PCE /api/v1/tasks（R88: 认证传播）。

PRD v2.3.1 §3 — Gate F3 调度闭环 + 认证传播。
PCE 第88轮 Gate A2 后所有 Task API 需 JWT Authorization header 和 X-Tenant-ID。

已验证端点（部署于 120.79.20.232:8180）：
- POST /api/v1/tasks
- GET  /api/v1/tasks
- POST /api/v1/tasks/{id}/status (未暴露，404 降级)
- POST /api/v1/tasks/{id}/checkpoint (未暴露，404 降级)

设计原则：
- 认证头从 PCE_JWT_TOKEN 环境变量读取，缺失时降级（不抛异常）
- 所有调用失败均优雅降级，返回 None/False，绝不阻断主 listing 流程
"""

import logging
import os

import requests

logger = logging.getLogger("pce-task-client")

PCE_API_BASE = os.environ.get("PCE_API_BASE", "http://127.0.0.1:8180")
TASKS_ENDPOINT = f"{PCE_API_BASE}/api/v1/tasks"
TASK_TIMEOUT = int(os.environ.get("PCE_TASK_TIMEOUT", "10"))
PCE_JWT_TOKEN = os.environ.get("PCE_JWT_TOKEN", "")
PCE_TENANT_ID = os.environ.get("PCE_TENANT_ID", "system")


def _build_headers() -> dict:
    """构建包含 JWT 认证和租户标识的请求头。

    无 JWT token 时跳过 Authorization header（降级模式，依赖 PCE 白名单或本地开发配置）。
    """
    headers = {
        "Content-Type": "application/json",
        "X-Tenant-ID": PCE_TENANT_ID,
    }
    if PCE_JWT_TOKEN:
        headers["Authorization"] = f"Bearer {PCE_JWT_TOKEN}"
    else:
        logger.warning("PCE_JWT_TOKEN 未配置，将发送匿名请求（可能被 PCE 拒绝）")
    return headers


def create_task(
    task_type: str,
    agent: str = "listing-worker",
    payload: dict | None = None,
    idempotency_key: str | None = None,
) -> str | None:
    """在 PCE 创建 Task，返回 task_id。

    Args:
        task_type: 任务类型（必填，如 "listing_generation"）
        agent: 发起方（默认 listing-worker）
        payload: 业务负载（可选）
        idempotency_key: 幂等键（可选）。相同 key 重复请求返回相同 task_id。

    Returns:
        str | None: 成功返回 task_id，失败降级返回 None
    """
    body = {"agent": agent, "task_type": task_type}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    if payload:
        body["payload"] = payload

    try:
        resp = requests.post(
            TASKS_ENDPOINT,
            json=body,
            headers=_build_headers(),
            timeout=TASK_TIMEOUT,
        )
        if resp.ok:
            data = resp.json()
            tid = data.get("task_id")
            if tid:
                logger.info(f"PCE CreateTask OK: {tid} (type={task_type})")
                return tid
            logger.warning(f"PCE CreateTask 返回无 task_id: {data}")
        else:
            logger.warning(f"PCE CreateTask HTTP {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"PCE CreateTask 失败（降级）: {e}")
    return None


def update_task_status(
    task_id: str,
    status: str,
    detail: dict | None = None,
) -> bool:
    """更新 Task 状态（best-effort）。

    当前 PCE 子端点未暴露（返回 404），调用优雅降级。
    Args:
        task_id: CreateTask 返回的 ID
        status: 目标状态（pending/running/completed/failed/cancelled）
        detail: 附加信息（可选）
    Returns:
        bool: 成功 True，降级或未暴露 False
    """
    if not task_id:
        return False

    url = f"{TASKS_ENDPOINT}/{task_id}/status"
    body = {"status": status}
    if detail:
        body["detail"] = detail

    try:
        resp = requests.post(url, json=body, headers=_build_headers(), timeout=TASK_TIMEOUT)
        if resp.ok:
            logger.info(f"PCE UpdateTaskStatus OK: {task_id} -> {status}")
            return True
        if resp.status_code == 404:
            logger.info(f"PCE UpdateTaskStatus 未暴露（404，降级跳过）: {task_id}")
            return False
        logger.warning(f"PCE UpdateTaskStatus HTTP {resp.status_code}（降级）: {resp.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"PCE UpdateTaskStatus 失败（降级）: {e}")
    return False


def task_checkpoint(
    task_id: str,
    phase: str,
    detail: dict | None = None,
) -> bool:
    """记录阶段 checkpoint（best-effort，404 降级）。

    Args:
        task_id: CreateTask 返回的 ID
        phase: 阶段名（如 "evidence_collected" / "react_done"）
        detail: 附加信息（可选）
    Returns:
        bool: 成功 True，降级或未暴露 False
    """
    if not task_id:
        return False

    url = f"{TASKS_ENDPOINT}/{task_id}/checkpoint"
    body = {"phase": phase}
    if detail:
        body["detail"] = detail

    try:
        resp = requests.post(url, json=body, headers=_build_headers(), timeout=TASK_TIMEOUT)
        if resp.ok:
            logger.info(f"PCE TaskCheckpoint OK: {task_id} @ {phase}")
            return True
        if resp.status_code == 404:
            logger.info(f"PCE TaskCheckpoint 未暴露（404，降级跳过）: {task_id}")
            return False
        logger.warning(f"PCE TaskCheckpoint HTTP {resp.status_code}（降级）: {resp.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"PCE TaskCheckpoint 失败（降级）: {e}")
    return False
