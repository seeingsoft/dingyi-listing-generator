"""Listing 侧真实 Dispatcher（R112-RECOVERY-GATE-D2）。

PRD v2.5 CTRL-01~04 - 真实调度。
async 模式不再只返回 accepted=true，而是入队 -> worker 消费 -> 实际执行 -> 更新 status。

使用 ThreadPoolExecutor（受限线程池，非无限 daemon）实现真实调度。
与 PCE Task 状态机联动：CreateTask -> 本地入队 -> worker 执行 -> update status。
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from task_state import (
    add_checkpoint,
    create_local_task,
    set_status,
)

logger = logging.getLogger("dispatcher")

# 受限线程池（最多 2 个 worker，非无限 daemon）
_MAX_WORKERS = int(os.environ.get("LISTING_DISPATCHER_WORKERS", "2"))
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# 活跃任务计数（用于健康检查）
_active_count = 0
_active_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS,
                thread_name_prefix="listing-dispatcher",
            )
            logger.info(
                f"[dispatcher] ThreadPoolExecutor started (max_workers={_MAX_WORKERS})"
            )
    return _executor


def _inc_active() -> None:
    global _active_count
    with _active_lock:
        _active_count += 1


def _dec_active() -> None:
    global _active_count
    with _active_lock:
        _active_count -= 1


def get_active_count() -> int:
    """返回当前正在执行的 task 数量。"""
    with _active_lock:
        return _active_count


def enqueue(
    task_id: str,
    tenant_id: str,
    payload: dict,
    work_fn: Callable[[dict], dict],
) -> bool:
    """入队一个异步任务。

    Args:
        task_id: 任务 ID
        tenant_id: 租户 ID
        payload: 业务负载
        work_fn: 执行函数，接收 payload，返回 result dict

    Returns:
        bool: 入队成功 True
    """
    create_local_task(task_id, tenant_id, payload)

    def _run() -> None:
        _inc_active()
        try:
            set_status(task_id, "running")
            add_checkpoint(task_id, "dispatch_started")

            result = work_fn(payload)
            add_checkpoint(task_id, "work_completed", {"result_keys": list(result.keys()) if isinstance(result, dict) else []})

            set_status(task_id, "completed", result=result)
            add_checkpoint(task_id, "task_finalized")

        except Exception as e:
            logger.error(f"[dispatcher] Task {task_id} failed: {e}", exc_info=True)
            set_status(task_id, "failed", error=str(e))
            add_checkpoint(task_id, "task_failed", {"error": str(e)})
        finally:
            _dec_active()

    try:
        _get_executor().submit(_run)
        logger.info(f"[dispatcher] Enqueued task {task_id} (tenant={tenant_id})")
        return True
    except Exception as e:
        logger.error(f"[dispatcher] Failed to enqueue {task_id}: {e}")
        set_status(task_id, "failed", error=f"dispatch failed: {e}")
        return False


def shutdown() -> None:
    """关闭 dispatcher（优雅退出）。"""
    global _executor
    with _executor_lock:
        if _executor:
            _executor.shutdown(wait=True, timeout=10)
            _executor = None
            logger.info("[dispatcher] ThreadPoolExecutor shut down")
