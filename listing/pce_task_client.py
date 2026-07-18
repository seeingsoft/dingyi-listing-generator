"""PCE Task 状态机客户端 - 对接 PCE /api/v1/tasks（R112-RECOVERY: 请求级 tenant）。

PRD v2.5 CTRL-01~04 / CTX-01~04 - 真实调度 + 请求级 Tenant 传播。
R112-RECOVERY: tenant_id 从请求级传入，不再使用进程级 os.environ。

已验证端点（部署于 120.79.20.232:8180）：
- POST /api/v1/tasks
- GET  /api/v1/tasks
- POST /api/v1/tasks/{id}/status (PCE 未暴露，404 降级 -> Listing 侧自维护)
- POST /api/v1/tasks/{id}/checkpoint (PCE 未暴露，404 降级 -> Listing 侧自维护)

设计原则：
- JWT token 从 PCE_JWT_TOKEN 环境变量读取（认证凭证，非业务租户）
- tenant_id 由调用方请求级传入（业务租户标识）
- 所有调用失败均优雅降级，返回 None/False，绝不阻断主 listing 流程
"""

import logging
import os

import requests

logger = logging.getLogger("pce-task-client")

PCE_API_BASE = os.environ.get("PCE_API_BASE", "http://127.0.0.1:8180")
TASKS_ENDPOINT = f"{PCE_API_BASE}/api/v1/tasks"
TASK_TIMEOUT = int(os.environ.get("PCE_TASK_TIMEOUT", "10"))
_cached_service_token = None


def _get_service_token() -> str:
    """获取 PCE service JWT token。

    优先级：
    1. PCE_JWT_TOKEN 环境变量（预签名 token）
    2. 自动签发（用 ENGINE_JWT_SECRET 签发 service token）
    """
    global _cached_service_token
    if _cached_service_token:
        return _cached_service_token

    env_token = os.environ.get("PCE_JWT_TOKEN", "")
    if env_token:
        _cached_service_token = env_token
        return _cached_service_token

    # Auto-generate service token using ENGINE_JWT_SECRET
    secret = os.environ.get("ENGINE_JWT_SECRET", "")
    if not secret:
        logger.warning("Neither PCE_JWT_TOKEN nor ENGINE_JWT_SECRET configured")
        return ""

    try:
        import jwt as _jwt
        import time as _time
        _cached_service_token = _jwt.encode(
            {
                "sub": "listing-service",
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "roles": ["admin"],
                "exp": int(_time.time()) + 86400 * 7,
            },
            secret,
            algorithm="HS256",
        )
        logger.info("Auto-generated service JWT from ENGINE_JWT_SECRET")
        return _cached_service_token
    except Exception as e:
        logger.warning(f"Failed to auto-generate service JWT: {e}")
        return ""


def _build_headers(tenant_id: str | None = None) -> dict:
    """构建包含 JWT 认证和请求级租户标识的请求头。"""
    headers = {"Content-Type": "application/json"}
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    token = _get_service_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("No service token available (PCE will reject)")
    return headers


def create_task(
    task_type: str,
    agent: str = "listing-worker",
    payload: dict | None = None,
    idempotency_key: str | None = None,
    tenant_id: str | None = None,
) -> str | None:
    """在 PCE 创建 Task，返回 task_id。

    Args:
        task_type: 任务类型（必填，如 "listing_generation"）
        agent: 发起方（默认 listing-worker）
        payload: 业务负载（可选）
        idempotency_key: 幂等键（可选）。相同 key 重复请求返回相同 task_id。
        tenant_id: 请求级租户标识（R112-RECOVERY，从 X-Tenant-ID 传入）

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
            headers=_build_headers(tenant_id=tenant_id),
            timeout=TASK_TIMEOUT,
        )
        if resp.ok:
            data = resp.json()
            tid = data.get("task_id")
            if tid:
                logger.info(f"PCE CreateTask OK: {tid} (type={task_type}, tenant={tenant_id})")
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
    tenant_id: str | None = None,
) -> bool:
    """更新 Task 状态（best-effort）。

    PCE 子端点未暴露（返回 404），调用优雅降级。
    Args:
        task_id: CreateTask 返回的 ID
        status: 目标状态（pending/running/completed/failed/cancelled）
        detail: 附加信息（可选）
        tenant_id: 请求级租户标识
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
        resp = requests.post(url, json=body, headers=_build_headers(tenant_id=tenant_id), timeout=TASK_TIMEOUT)
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
    tenant_id: str | None = None,
) -> bool:
    """记录阶段 checkpoint（best-effort，404 降级）。

    Args:
        task_id: CreateTask 返回的 ID
        phase: 阶段名（如 "evidence_collected" / "react_done"）
        detail: 附加信息（可选）
        tenant_id: 请求级租户标识
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
        resp = requests.post(url, json=body, headers=_build_headers(tenant_id=tenant_id), timeout=TASK_TIMEOUT)
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


def submit_approval(task_id: str, task_type: str = "listing_generation", tenant_id: str | None = None) -> bool:
    """R22-FIX: 向 PCE 提交审批（approval_required 任务生成完成后调用）。

    PCE policy engine 按 task_type 判定 trust level：
    listing_generation -> L3 -> 人工审批（approval_queue pending）。
    """
    if not task_id:
        return False
    url = PCE_API_BASE + "/api/v1/approvals/submit"
    try:
        resp = requests.post(url, json={"task_id": task_id, "task_type": task_type},
                             headers=_build_headers(tenant_id=tenant_id), timeout=TASK_TIMEOUT)
        if resp.ok:
            logger.info(f"PCE SubmitApproval OK: {task_id}")
            return True
        logger.warning(f"PCE SubmitApproval HTTP {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"PCE SubmitApproval failed: {e}")
    return False
