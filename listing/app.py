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

# === Flask 初始化 ===
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["JSON_SORT_KEYS"] = False

# === 日志 ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("listing")


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
                "endpoint": os.environ.get("PCE_API_BASE", "http://localhost:8080"),
                "error": pce_error,
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
    base_quality = 85  # LLM 基础分
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
    final_quality = 0 if has_prohibited else min(85, compliance_score)

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
    """通过 ReAct 多工具 Agent 生成 Listing。

    Request Body: 同 /api/v1/listing/generate，额外支持:
        "competitor_asins": [...],  # 可选，自动通过 ISR 分析竞品
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

    if not product_name:
        raise APIError("product_name is required", code=400)

    if not isinstance(keywords, list):
        keywords = [keywords]
    if not isinstance(selling_points, list):
        selling_points = [selling_points]
    if not isinstance(competitor_asins, list):
        competitor_asins = [competitor_asins] if competitor_asins else []

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
        raise APIError(f"ReAct agent failed: {e}", code=502)

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
    final_quality = 0 if has_prohibited else min(85, compliance_score)

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
    })


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
            "quality_score": 85,
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
