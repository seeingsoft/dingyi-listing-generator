#!/usr/bin/env python3
"""PRD v2.2 Phase 1 P0-1完善 — Pro 独立评审器（质量闭环）。

闭环设计：Flash 模型生成 listing → Pro 模型（deepseek-v4-pro）独立评审 → 质量对比。

调用 PCE /api/v1/llm/call（model_hint="pro"），对 Flash 生成的 listing 进行
独立合规 + 质量评审，输出 reviewer_quality_score 并与 Flash 质量分对比。
PCE 不可用时返回 error（禁止直连外部 LLM API）。
"""

import json
import logging
import os

import requests

logger = logging.getLogger("quality-reviewer")

PCE_API_BASE = os.environ.get("PCE_API_BASE", "http://127.0.0.1:8180")
PCE_CALL_ENDPOINT = f"{PCE_API_BASE}/api/v1/llm/call"
LISTING_TAG = "listing-generation"
PRO_TIMEOUT = int(os.environ.get("PRO_REVIEW_TIMEOUT", "40"))


def pro_review(
    listing: dict,
    product_name: str = "",
    category: str = "",
    target_market: str = "US",
    language: str = "en",
    flash_quality_score: int | None = None,
) -> dict:
    """Pro 模型独立评审 Flash 生成的 listing。

    Args:
        listing: {title, bullets[], description, search_terms[]}
        product_name / category / target_market / language: 产品上下文
        flash_quality_score: Flash 路径算出的质量分（用于对比）

    Returns:
        dict: {
            "reviewer_quality_score": int | None,
            "passed": bool,
            "issues": list[str],
            "comparison": dict,        # flash vs pro 对比
            "summary": str,
            "raw": str,
            "error": str | None,
        }
    """
    result: dict = {
        "reviewer_quality_score": None,
        "passed": False,
        "issues": [],
        "comparison": {},
        "summary": "",
        "raw": "",
        "error": None,
    }

    title = listing.get("title", "")
    bullets = listing.get("bullets", []) or []
    description = listing.get("description", "")

    system_prompt = (
        "You are a senior Amazon listing quality auditor (Pro tier). "
        "Independently evaluate the provided listing for compliance, accuracy, "
        "benefit-driven copy, and Amazon Cosmo algorithm fit. "
        "Be strict but fair. "
        'Output ONLY valid JSON: '
        '{"reviewer_quality_score": <int 0-100>, "passed": <bool>, '
        '"issues": [<string>], "summary": "<string>"}'
    )
    user_prompt = (
        f"Product: {product_name}\n"
        f"Category: {category}\n"
        f"Market: {target_market}\n\n"
        f"Title: {title}\n\n"
        f"Bullets:\n" + "\n".join(f"- {b}" for b in bullets) + "\n\n"
        f"Description: {description}\n\n"
        "Audit this listing independently. Score 0-100. "
        "Flag any compliance, quality, or accuracy issues as concise strings."
    )

    body = {
        "tag": LISTING_TAG,
        "model_hint": "pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "temperature": 0.0,
            "max_tokens": 2000,
            "timeout_ms": PRO_TIMEOUT * 1000,
        },
    }

    try:
        resp = requests.post(PCE_CALL_ENDPOINT, json=body, timeout=PRO_TIMEOUT)
        if not resp.ok:
            result["error"] = f"PCE HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning(result["error"])
            return result

        data = resp.json()
        if not data.get("success"):
            result["error"] = f"PCE call failed: {data.get('error', 'unknown')}"
            logger.warning(result["error"])
            return result

        content = data["data"].get("content", "")
        json_data = data["data"].get("json")
        parsed = json_data if json_data else _extract_json(content)
    except Exception as e:
        result["error"] = f"Pro 评审 PCE 调用失败: {e}"
        logger.warning(result["error"])
        return result

    if not parsed:
        result["error"] = "Pro 评审未返回可解析 JSON"
        result["raw"] = content[:500]
        logger.warning(result["error"])
        return result

    score = parsed.get("reviewer_quality_score", parsed.get("quality_score"))
    result["reviewer_quality_score"] = int(score) if score is not None else None
    result["passed"] = bool(parsed.get("passed", False))
    result["issues"] = parsed.get("issues", []) or []
    result["summary"] = parsed.get("summary", "")
    result["raw"] = content[:500]

    # Flash vs Pro 质量对比
    if flash_quality_score is not None and result["reviewer_quality_score"] is not None:
        delta = result["reviewer_quality_score"] - flash_quality_score
        result["comparison"] = {
            "flash_score": flash_quality_score,
            "pro_score": result["reviewer_quality_score"],
            "delta": delta,
            "verdict": (
                "consistent"
                if abs(delta) <= 10
                else ("pro_higher" if delta > 0 else "flash_higher")
            ),
        }

    return result


def _extract_json(content: str) -> dict | None:
    """从 LLM 文本响应中提取 JSON。"""
    if not content:
        return None
    if "```json" in content:
        s = content.index("```json") + 7
        e = content.index("```", s)
        content = content[s:e]
    elif "```" in content:
        s = content.index("```") + 3
        e = content.index("```", s)
        content = content[s:e]
    try:
        return json.loads(content.strip())
    except (json.JSONDecodeError, ValueError):
        return None
