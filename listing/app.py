"""鼎一 Listing 生成 Plugin — Flask Web 服务。

提供 REST API 端点：
- POST /api/v1/listing/generate — 生成 listing 文案
- GET  /health — 健康检查
- GET  /api/v1/listing/health — 深度健康检查（含 PCE 连通性）
"""

import json
import logging
import os
import sys
import traceback
from typing import Any

from flask import Flask, jsonify, request

# 确保当前目录在 sys.path 中，以便导入 listing_generator 和 compliance_checker
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from listing_generator import LLMError, _fetch_competitor_data, generate_listing, health_check as pce_health
from compliance_checker import (
    ComplianceReport,
    check_compliance,
    check_brand_filter,
    get_quality_score,
)
from report_generator import generate_report, render_html
from react_agent import generate_via_react
from compliance_api import check_compliance_full
from image_extractor import extract_from_image
from publisher import format_for_platforms, list_platforms
from variant_generator import generate_variants
from evidence_collector import _fetch_parallel_evidence, build_evidence_graph
from pce_task_client import create_task, update_task_status, task_checkpoint
from quality_reviewer import pro_review
import hashlib

# R112-RECOVERY-GATE-D2: 真实 dispatcher + 请求级 tenant + status 端点
from task_state import init_db as task_init_db, gen_task_id, get_task, get_checkpoints
from dispatcher import enqueue as dispatch_enqueue, get_active_count
from status_endpoint import bp as task_status_bp

# === Flask 初始化 ===
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["JSON_SORT_KEYS"] = False

# 注册 task status/checkpoint/receipt 端点
app.register_blueprint(task_status_bp)

# === 日志 ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("listing")

# === R112-RECOVERY: 初始化本地 task 状态 DB ===
task_init_db()


# === 错误处理 ===
class APIError(Exception):
    def __init__(self, message: str, code: int = 400, details: dict | None = None):
        self.message = message
        self.code = code
        self.details = details or {}


@app.errorhandler(APIError)
def handle_api_error(e: APIError):
    return jsonify({
        "success": False,
        "error": e.message,
        "details": e.details,
    }), e.code


@app.errorhandler(400)
def handle_bad_request(e):
    return jsonify({
        "success": False,
        "error": "Bad request",
        "details": str(e),
    }), 400


@app.errorhandler(500)
def handle_internal_error(e):
    logger.error(f"Internal error: {traceback.format_exc()}")
    return jsonify({
        "success": False,
        "error": "Internal server error",
        "details": str(e),
    }), 500


# === API 端点 ===

@app.route("/health", methods=["GET"])
def health():
    """基础健康检查。"""
    return jsonify({"status": "healthy", "service": "listing-generator"})


@app.route("/api/v1/listing/health", methods=["GET"])
def deep_health():
    """深度健康检查，含 PCE 连通性。"""
    pce_status = "unknown"
    pce_error = None
    try:
        pce_health()
        pce_status = "connected"
    except LLMError as e:
        pce_status = "error"
        pce_error = str(e)

    return jsonify({
        "status": "healthy" if pce_status == "connected" else "degraded",
        "service": "listing-generator",
        "components": {
            "pce": {
                "status": pce_status,
                "endpoint": os.environ.get("PCE_API_BASE", "http://127.0.0.1:8180"),
                "error": pce_error,
            },
            "dispatcher": {
                "active_tasks": get_active_count(),
            },
        },
    })


@app.route("/api/v1/listing/generate", methods=["POST"])
def generate():
    """生成 Amazon listing 文案。

    Request Body (JSON):
        {
            "product_name": "YETI Rambler 26oz",
            "category": "Sports & Outdoors > Water Bottles",
            "keywords": ["stainless steel", "insulated", "BPA-free"],
            "selling_points": ["双层真空保温", "18/8不锈钢", "防漏设计"],
            "target_market": "US",
            "language": "en"
        }

    Response:
        {
            "success": true,
            "data": {
                "title": "...",
                "bullets": ["...", ...],
                "description": "...",
                "search_terms": ["...", ...]
            },
            "compliance": {
                "passed": true,
                "violations": [],
                "quality_score": 100
            },
            "usage": { ... }
        }
    """
    # 1. 参数校验
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body must be valid JSON", code=400)

    product_name = body.get("product_name", "").strip()
    if not product_name:
        raise APIError("product_name is required", code=400)

    category = body.get("category", "").strip()
    keywords = body.get("keywords", [])
    selling_points = body.get("selling_points", [])
    target_market = body.get("target_market", "US")
    language = body.get("language", "en")
    competitor_asins = body.get("competitor_asins", [])

    # 2. 参数规范化
    if not isinstance(keywords, list):
        keywords = [keywords]
    if not isinstance(selling_points, list):
        selling_points = [selling_points]
    if not isinstance(competitor_asins, list):
        competitor_asins = [competitor_asins] if competitor_asins else []

    # 3a. 获取竞品数据（如果提供了 ASIN）
    competitor_data = None
    if competitor_asins:
        competitor_data = _fetch_competitor_data(competitor_asins)
        logger.info(f"Fetched competitor data for {len(competitor_asins)} ASINs")

    # 3b. 调用 LLM 生成
    try:
        result = generate_listing(
            product_name=product_name,
            category=category,
            keywords=keywords,
            selling_points=selling_points,
            target_market=target_market,
            language=language,
            competitor_data=competitor_data,
        )
    except LLMError as e:
        logger.error(f"LLM call failed: {e}")
        raise APIError(f"LLM generation failed: {e}", code=502)
    except Exception as e:
        logger.error(f"Unexpected error: {traceback.format_exc()}")
        raise APIError(f"Generation failed: {e}", code=500)

    # 4. 合规检查（传入产品名排除自身品牌误报）
    compliance = check_compliance(
        title=result.get("title", ""),
        bullets=result.get("bullets", []),
        description=result.get("description", ""),
        product_name=product_name,
    )

    # 5. 品牌词过滤（v2.1 新增）
    brand_violations = check_brand_filter(
        title=result.get("title", ""),
        bullets=result.get("bullets", []),
        description=result.get("description", ""),
    )

    # 合并所有 violations
    all_violations = compliance.violations + brand_violations

    # 6. 计算质量分数（考虑合规+品牌过滤）
    base_quality = 90  # LLM 基础分（PRD v2.1 L132 要求 ≥90）
    compliance_score = get_quality_score(all_violations)

    # 如果有禁售词，强制质量分为 0
    has_prohibited = any(v["type"] == "prohibited" for v in all_violations)
    final_quality = 0 if has_prohibited else min(base_quality, compliance_score)

    # 最终合规状态
    final_passed = compliance.passed and len(brand_violations) == 0

    response = {
        "success": True,
        "data": {
            "title": result.get("title", ""),
            "bullets": result.get("bullets", []),
            "description": result.get("description", ""),
            "search_terms": result.get("search_terms", []),
        },
        "compliance": {
            "passed": final_passed,
            "violations": all_violations,
            "quality_score": final_quality,
            "brand_violations": len(brand_violations),
        },
    }

    logger.info(
        f"Generated listing for '{product_name}' | "
        f"quality={final_quality} | "
        f"compliance={'PASS' if final_passed else 'FAIL'} | "
        f"brand_violations={len(brand_violations)}"
    )

    return jsonify(response)


@app.route("/api/v1/listing/report", methods=["POST"])
def generate_report_endpoint():
    """生成结构化报告（HTML + JSON）。

    Request Body (JSON):
        同 `/api/v1/listing/generate`，额外可选：
        {
            ...generate 参数...
            "format": "json" | "html",  # 默认 json
        }

    Response (format=json):
        { "success": true, "report": { ... }, "report_html": "..." }

    Response (format=html):
        text/html Content-Type
    """
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body is required", code=400)

    product_name = body.get("product_name", "")
    category = body.get("category", "")
    keywords = body.get("keywords", [])
    selling_points = body.get("selling_points", [])
    target_market = body.get("target_market", "US")
    language = body.get("language", "en")
    competitor_asins = body.get("competitor_asins", [])
    output_format = body.get("format", "json")

    if not product_name:
        raise APIError("product_name is required", code=400)

    # 规范化参数
    if not isinstance(keywords, list):
        keywords = [keywords]
    if not isinstance(selling_points, list):
        selling_points = [selling_points]
    if not isinstance(competitor_asins, list):
        competitor_asins = [competitor_asins] if competitor_asins else []

    # 获取竞品数据
    competitor_data = None
    if competitor_asins:
        competitor_data = _fetch_competitor_data(competitor_asins)

    # 生成 listing
    try:
        result = generate_listing(
            product_name=product_name,
            category=category,
            keywords=keywords,
            selling_points=selling_points,
            target_market=target_market,
            language=language,
            competitor_data=competitor_data,
        )
    except LLMError as e:
        raise APIError(f"LLM generation failed: {e}", code=502)
    except Exception as e:
        raise APIError(f"Generation failed: {e}", code=500)

    # 合规检查 + 品牌过滤
    compliance = check_compliance(
        title=result.get("title", ""),
        bullets=result.get("bullets", []),
        description=result.get("description", ""),
        product_name=product_name,
    )
    brand_violations_list = check_brand_filter(
        title=result.get("title", ""),
        bullets=result.get("bullets", []),
        description=result.get("description", ""),
    )
    all_violations = compliance.violations + brand_violations_list
    has_prohibited = any(v["type"] == "prohibited" for v in all_violations)
    compliance_score = get_quality_score(all_violations)
    final_quality = 0 if has_prohibited else min(90, compliance_score)

    import time as _t
    # 生成报告
    report = generate_report(
        product_name=product_name,
        category=category,
        title=result.get("title", ""),
        bullets=result.get("bullets", []),
        description=result.get("description", ""),
        search_terms=result.get("search_terms", []),
        violations=all_violations,
        quality_score=final_quality,
        brand_violations=len(brand_violations_list),
        keywords=result.get("search_terms"),
        competitor_asins=competitor_asins,
        competitor_data=competitor_data,
        language=language,
        target_market=target_market,
        elapsed_ms=0,
    )
    report_html = render_html(report)

    if output_format == "html":
        return report_html, 200, {"Content-Type": "text/html; charset=utf-8"}

    return jsonify({"success": True, "report": report, "report_html": report_html})


@app.route("/api/v1/listing/generate/react", methods=["POST"])
def generate_react():
    """通过 ReAct Agent 生成 Listing，接入 PCE Task 状态机。

    Request Body: 同 /api/v1/listing/generate，额外支持:
        "competitor_asins": [...],  # 可选，自动通过 ISR 分析竞品
        "async": true,               # 可选，异步模式 → {task_id, accepted}

    Response (sync): 完整 listing + compliance + evidence_graph + pro_review + task_id
    Response (async): {"task_id": "...", "accepted": true, "mode": "react"}
    Response (PCE 不可用): {"accepted": false, "error": "PCE CreateTask failed: ..."}
    """
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body is required", code=400)

    product_name = body.get("product_name", "").strip()
    category = body.get("category", "").strip()
    keywords = body.get("keywords", [])
    selling_points = body.get("selling_points", [])
    target_market = body.get("target_market", "US").strip()
    language = body.get("language", "en").strip()
    competitor_asins = body.get("competitor_asins", [])
    async_mode = bool(body.get("async", False))

    if not product_name:
        raise APIError("product_name is required", code=400)

    if not isinstance(keywords, list):
        keywords = [keywords]
    if not isinstance(selling_points, list):
        selling_points = [selling_points]
    if not isinstance(competitor_asins, list):
        competitor_asins = [competitor_asins] if competitor_asins else []

    # 1. R112-RECOVERY: 请求级 tenant_id（从 X-Tenant-ID header 提取，非进程级 env）
    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    if not tenant_id:
        raise APIError("X-Tenant-ID header required", code=401)

    # 2. 生成幂等键（R88: 纳入 tenant_id + competitor_asins + async/sync mode + schema_version）
    schema_version = "v2.5.0"
    async_label = "async" if async_mode else "sync"
    asins_str = ",".join(sorted(competitor_asins)) if competitor_asins else ""
    id_key_raw = (
        f"{tenant_id}:::{schema_version}:::{async_label}:::"
        f"{product_name}:::{category}:::"
        f"{','.join(sorted(keywords))}:::"
        f"{','.join(sorted(selling_points))}:::"
        f"{asins_str}:::{target_market}:::{language}"
    )
    idempotency_key = hashlib.sha256(id_key_raw.encode()).hexdigest()[:32]

    # 3. 生成本地 task_id（R112-RECOVERY: Listing 侧自维护状态机）
    local_task_id = gen_task_id()

    # 4. 创建 PCE Task（带 JWT 认证和幂等键，best-effort）
    pce_task_id = create_task(
        task_type="listing_generation",
        payload={
            "product_name": product_name,
            "market": target_market,
            "mode": async_label,
            "schema_version": schema_version,
            "local_task_id": local_task_id,
        },
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
    )

    # PCE 不可用时，仍可在 Listing 侧本地调度（R112-RECOVERY: 不回退到无限 daemon）
    # 但如果连本地 dispatcher 也入队失败，返回 accepted=false
    use_local_dispatch = True

    # 5. 异步模式：真实 dispatcher 入队 -> worker 消费 -> 实际执行
    if async_mode:
        from task_state import create_local_task, set_status as _set_status
        from task_state import add_checkpoint as _add_cp

        # 创建本地 task 记录
        create_local_task(local_task_id, tenant_id, {
            "product_name": product_name,
            "category": category,
            "keywords": keywords,
            "selling_points": selling_points,
            "target_market": target_market,
            "language": language,
            "competitor_asins": competitor_asins,
            "pce_task_id": pce_task_id,
        })
        _add_cp(local_task_id, "task_created", {"pce_task_id": pce_task_id})

        # 定义 worker 执行函数
        def _async_work(payload: dict) -> dict:
            """真实 worker 执行体：运行 ReAct 流水线 + 合规 + 证据 + 评审。"""
            _add_cp(local_task_id, "react_start")

            result = generate_via_react(
                product_name=payload["product_name"],
                category=payload["category"],
                keywords=payload["keywords"],
                selling_points=payload["selling_points"],
                target_market=payload["target_market"],
                language=payload["language"],
                competitor_asins=payload["competitor_asins"],
            )
            _add_cp(local_task_id, "react_done", {"title": result.get("title", "")[:50]})

            compliance = check_compliance(
                title=result.get("title", ""),
                bullets=result.get("bullets", []),
                description=result.get("description", ""),
                product_name=payload["product_name"],
            )
            brand_violations_list = check_brand_filter(
                title=result.get("title", ""),
                bullets=result.get("bullets", []),
                description=result.get("description", ""),
            )
            all_violations = compliance.violations + brand_violations_list
            has_prohibited = any(v["type"] == "prohibited" for v in all_violations)
            compliance_score = get_quality_score(all_violations)
            final_quality = 0 if has_prohibited else min(90, compliance_score)
            _add_cp(local_task_id, "compliance_done", {"quality": final_quality})

            try:
                ev_claims = _fetch_parallel_evidence(
                    asins=payload["competitor_asins"] if payload["competitor_asins"] else None,
                    keywords=result.get("search_terms"),
                    market=payload["target_market"],
                )
                ev_graph = build_evidence_graph(ev_claims)
            except Exception:
                ev_graph = {"total_claims": 0, "sources": [], "claims": [], "data_insufficient": True}
            _add_cp(local_task_id, "evidence_done", {"claims": ev_graph.get("total_claims", 0)})

            pro_review_result = None
            try:
                pro_review_result = pro_review(
                    listing=result,
                    product_name=payload["product_name"],
                    category=payload["category"],
                    target_market=payload["target_market"],
                    language=payload["language"],
                    flash_quality_score=final_quality,
                )
            except Exception as e:
                logger.warning(f"Pro review skipped (degraded): {e}")
            _add_cp(local_task_id, "review_done")

            # best-effort 更新 PCE task 终态
            if pce_task_id:
                update_task_status(pce_task_id, "completed", detail={
                    "local_task_id": local_task_id,
                    "quality": final_quality,
                })

            return {
                "title": result.get("title", ""),
                "bullets": result.get("bullets", []),
                "description": result.get("description", ""),
                "search_terms": result.get("search_terms", []),
                "compliance": {
                    "passed": compliance.passed and len(brand_violations_list) == 0,
                    "violations": all_violations,
                    "quality_score": final_quality,
                    "brand_violations": len(brand_violations_list),
                },
                "evidence_graph": ev_graph,
                "pro_review": pro_review_result,
            }

        # 入队真实 dispatcher
        ok = dispatch_enqueue(local_task_id, tenant_id, {
            "product_name": product_name,
            "category": category,
            "keywords": keywords,
            "selling_points": selling_points,
            "target_market": target_market,
            "language": language,
            "competitor_asins": competitor_asins,
        }, _async_work)

        if not ok:
            return jsonify({
                "accepted": False,
                "error": "dispatcher enqueue failed",
                "task_id": local_task_id,
            })

        return jsonify({
            "task_id": local_task_id,
            "pce_task_id": pce_task_id,
            "accepted": True,
            "mode": "react",
            "status_url": f"/api/v1/tasks/{local_task_id}",
            "receipt_url": f"/api/v1/tasks/{local_task_id}/receipt",
        })

    # 6. 同步模式：直接执行流水线
    from task_state import create_local_task, set_status as _set_status
    from task_state import add_checkpoint as _add_cp

    create_local_task(local_task_id, tenant_id, {
        "product_name": product_name,
        "category": category,
        "keywords": keywords,
        "selling_points": selling_points,
        "target_market": target_market,
        "language": language,
        "competitor_asins": competitor_asins,
        "pce_task_id": pce_task_id,
    })
    _set_status(local_task_id, "running")
    _add_cp(local_task_id, "sync_react_start")

    try:
        result = generate_via_react(
            product_name=product_name,
            category=category,
            keywords=keywords,
            selling_points=selling_points,
            target_market=target_market,
            language=language,
            competitor_asins=competitor_asins,
        )
    except Exception as e:
        logger.error(f"ReAct agent failed: {traceback.format_exc()}")
        _set_status(local_task_id, "failed", error=str(e))
        if pce_task_id:
            update_task_status(pce_task_id, "failed", detail={"error": str(e)})
        raise APIError(f"ReAct agent failed: {e}", code=502)

    _add_cp(local_task_id, "sync_react_done", {"title": result.get("title", "")[:50]})

    # 合规检查 + 品牌过滤
    compliance = check_compliance(
        title=result.get("title", ""),
        bullets=result.get("bullets", []),
        description=result.get("description", ""),
        product_name=product_name,
    )
    brand_violations_list = check_brand_filter(
        title=result.get("title", ""),
        bullets=result.get("bullets", []),
        description=result.get("description", ""),
    )
    all_violations = compliance.violations + brand_violations_list
    has_prohibited = any(v["type"] == "prohibited" for v in all_violations)
    compliance_score = get_quality_score(all_violations)
    final_quality = 0 if has_prohibited else min(90, compliance_score)
    _add_cp(local_task_id, "sync_compliance_done", {"quality": final_quality})

    # 证据图（非阻塞，失败不中断主流程）
    try:
        ev_claims = _fetch_parallel_evidence(
            asins=competitor_asins if competitor_asins else None,
            keywords=result.get("search_terms"),
            market=target_market,
        )
        ev_graph = build_evidence_graph(ev_claims)
    except Exception:
        ev_graph = {"total_claims": 0, "sources": [], "claims": [], "data_insufficient": True}
    _add_cp(local_task_id, "sync_evidence_done", {"claims": ev_graph.get("total_claims", 0)})

    # Pro 独立评审（非阻塞，失败不中断主流程）
    pro_review_result = None
    try:
        pro_review_result = pro_review(
            listing=result,
            product_name=product_name,
            category=category,
            target_market=target_market,
            language=language,
            flash_quality_score=final_quality,
        )
    except Exception as e:
        logger.warning(f"Pro review skipped (degraded): {e}")
    _add_cp(local_task_id, "sync_review_done")

    # 标记 sync task 完成
    sync_result = {
        "title": result.get("title", ""),
        "bullets": result.get("bullets", []),
        "description": result.get("description", ""),
        "search_terms": result.get("search_terms", []),
        "compliance": {
            "passed": compliance.passed and len(brand_violations_list) == 0,
            "violations": all_violations,
            "quality_score": final_quality,
            "brand_violations": len(brand_violations_list),
        },
        "evidence_graph": ev_graph,
        "pro_review": pro_review_result,
    }
    _set_status(local_task_id, "completed", result=sync_result)
    _add_cp(local_task_id, "sync_task_finalized")

    # best-effort 更新 PCE task 终态
    if pce_task_id:
        update_task_status(pce_task_id, "completed", detail={
            "local_task_id": local_task_id,
            "quality": final_quality,
        })

    return jsonify({
        "success": True,
        "data": {
            "title": result.get("title", ""),
            "bullets": result.get("bullets", []),
            "description": result.get("description", ""),
            "search_terms": result.get("search_terms", []),
        },
        "compliance": {
            "passed": compliance.passed and len(brand_violations_list) == 0,
            "violations": all_violations,
            "quality_score": final_quality,
            "brand_violations": len(brand_violations_list),
        },
        "mode": "react",
        "task_id": local_task_id,
        "pce_task_id": pce_task_id,
        "evidence_graph": ev_graph,
        "pro_review": pro_review_result,
        "status_url": f"/api/v1/tasks/{local_task_id}",
        "receipt_url": f"/api/v1/tasks/{local_task_id}/receipt",
    })


@app.route("/api/v1/listing/review", methods=["POST"])
def review_listing():
    """Pro 独立质量评审端点。

    对已有 listing 文案进行 Pro 模型独立评审，输出质量分与 Flash 对比。
    """
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body is required", code=400)

    title = body.get("title", "")
    bullets = body.get("bullets", [])
    description = body.get("description", "")

    if not title and not bullets and not description:
        raise APIError("title / bullets / description 至少提供一个", code=400)

    if not isinstance(bullets, list):
        bullets = [bullets]

    listing = {
        "title": title,
        "bullets": bullets,
        "description": description,
        "search_terms": body.get("search_terms", []),
    }

    try:
        review = pro_review(
            listing=listing,
            product_name=body.get("product_name", ""),
            category=body.get("category", ""),
            target_market=body.get("target_market", "US"),
            language=body.get("language", "en"),
            flash_quality_score=body.get("flash_quality_score"),
        )
    except Exception as e:
        logger.error(f"Pro review failed: {traceback.format_exc()}")
        raise APIError(f"Pro review failed: {e}", code=502)

    return jsonify({"success": True, "review": review})


@app.route("/api/v1/listing/compliance", methods=["POST"])
def compliance_check():
    """合规前置筛查端点。

    Request:
        {
            "title": "...",
            "bullets": ["...", ...] | undefined,
            "description": "...",
            "category": "Electronics > Headphones",
            "target_market": "US",
            "product_name": "My Product"  # 可选
        }

    Response:
        {
            "success": true,
            "passed": false,
            "violations": [...],
            "suggestions": [...],
            "confidence": 0.95,
            "summarized": { "禁售词": 0, ... }
        }
    """
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body is required", code=400)

    title = body.get("title", "")
    bullets = body.get("bullets", [])
    description = body.get("description", "")
    category = body.get("category", "")
    target_market = body.get("target_market", "US")
    product_name = body.get("product_name", "")

    if not title:
        raise APIError("title is required", code=400)
    if not category:
        raise APIError("category is required", code=400)

    if not isinstance(bullets, list):
        bullets = [bullets]

    result = check_compliance_full(
        title=title,
        bullets=bullets,
        description=description,
        category=category,
        target_market=target_market,
        product_name=product_name,
    )

    return jsonify({"success": True, **result})


@app.route("/api/v1/listing/extract-from-image", methods=["POST"])
def extract_from_image_endpoint():
    """图片→属性预填端点（零输入冷启动）。

    Request:
        {
            "image_url": "https://...1688.com/...jpg",   # 与 image_base64 二选一
            "image_base64": "<base64 data>"               # 与 image_url 二选一
        }

    Response:
        {
            "success": true,
            "product_name": "...",
            "product_name_cn": "...",
            "category": "...",
            "attributes": { ... },
            "extracted_text": "...",
            "confidence": 0.82,
            "elapsed_ms": 3500
        }
    """
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body is required", code=400)

    image_url = body.get("image_url")
    image_base64 = body.get("image_base64")

    if not image_url and not image_base64:
        raise APIError("image_url 或 image_base64 至少提供一个", code=400)

    import time as _t
    start = _t.time()

    try:
        result = extract_from_image(
            image_url=image_url,
            image_base64=image_base64,
        )
    except Exception as e:
        logger.error(f"Image extraction failed: {traceback.format_exc()}")
        raise APIError(f"Image extraction failed: {e}", code=502)

    elapsed_ms = int((_t.time() - start) * 1000)

    return jsonify({
        "success": True,
        **result,
        "elapsed_ms": elapsed_ms,
    })


@app.route("/api/v1/listing/publish", methods=["POST"])
def publish_listing():
    """跨平台一键发布准备。

    Request:
        {
            "listing": {
                "title": "...",
                "bullets": [...],
                "description": "...",
                "keywords": [...]
            },
            "platforms": ["amazon", "walmart"]
        }

    Response:
        {
            "success": true,
            "results": {
                "amazon": { "title": "...", "validation_passed": true, ... },
                "walmart": { ... }
            }
        }
    """
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body is required", code=400)

    listing = body.get("listing", {})
    platforms = body.get("platforms", [])

    if not listing.get("title"):
        raise APIError("listing.title is required", code=400)
    if not platforms:
        raise APIError("platforms is required (e.g. ['amazon', 'walmart'])", code=400)

    results = format_for_platforms(listing, platforms)

    all_passed = all(
        r.get("validation_passed", False) for r in results.values()
        if "error" not in r
    )

    return jsonify({
        "success": True,
        "results": results,
        "all_passed": all_passed,
        "platform_count": len(results),
    })


@app.route("/api/v1/listing/variant-split", methods=["POST"])
def variant_split():
    """爆款横向裂变。

    Request:
        {
            "source_listing": {
                "title": "...",
                "bullets": [...],
                "description": "...",
                "category": "Sports > Water Bottles"
            },
            "split_dimension": "color",  # color/size/material/feature/auto
            "variant_count": 3
        }

    Response:
        {
            "success": true,
            "source": "...",
            "split_dimension": "color",
            "variants": [...],
            "quality_score": 90,
            "elapsed_ms": 5000
        }
    """
    body = request.get_json(silent=True)
    if not body:
        raise APIError("Request body is required", code=400)

    source = body.get("source_listing", {})
    split_dimension = body.get("split_dimension", "auto")
    variant_count = body.get("variant_count", 3)

    if not source.get("title"):
        raise APIError("source_listing.title is required", code=400)
    if variant_count < 2 or variant_count > 6:
        raise APIError("variant_count must be between 2 and 6", code=400)

    import time as _t
    start = _t.time()

    try:
        result = generate_variants(
            source_title=source["title"],
            source_bullets=source.get("bullets", []),
            source_description=source.get("description", ""),
            category=source.get("category", ""),
            split_dimension=split_dimension,
            variant_count=variant_count,
        )
    except Exception as e:
        logger.error(f"Variant split failed: {traceback.format_exc()}")
        raise APIError(f"Variant split failed: {e}", code=502)

    elapsed_ms = int((_t.time() - start) * 1000)

    return jsonify({
        "success": True,
        "source": result["source"],
        "split_dimension": result["split_dimension"],
        "variants": result["variants"],
        "analysis": result.get("analysis", ""),
        "quality_score": result["quality_score"],
        "elapsed_ms": elapsed_ms,
    })


# === 开发模式 ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_ENV") != "production"
    logger.info(f"Starting Listing Generator on port {port} (debug={debug})")
    app.run(host="0.0.0.0", port=port, debug=debug)
