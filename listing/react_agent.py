"""ReAct 多工具 Agent — 数据驱动竞品反向工程五阶段流程。

优先调用 PCE A2A `/a2a/message:send`（agent=listing-worker），
失败时降级到本地实现（直接调用 ISR + LLM）。
"""

import json
import logging
import os
from typing import Optional

import requests

from listing_generator import generate_listing

logger = logging.getLogger("listing-react")

PCE_A2A_BASE = os.environ.get("PCE_A2A_BASE", "http://127.0.0.1:8180")
ISR_BASE = os.environ.get("ISR_API_BASE", "http://127.0.0.1:5000/api/v1")

# A2A 超时阈值：超过此时间则降级到 _react_local（降级路径 8.5s 可接受）
A2A_TIMEOUT = int(os.environ.get("A2A_TIMEOUT", "6"))


def generate_via_react(
    product_name: str,
    category: str,
    keywords: list[str],
    selling_points: list[str],
    target_market: str = "US",
    language: str = "en",
    competitor_asins: list[str] | None = None,
) -> dict:
    """通过 ReAct Agent 生成 Listing（多工具协同）。

    优先 PCE A2A /a2a/message:send（agent=listing-worker），
    PCE 不可用或 agent 未注册时降级到 _react_local。
    """
    asins_str = ", ".join(competitor_asins) if competitor_asins else "none"
    message = (
        f"Generate an optimized Amazon listing for '{product_name}' "
        f"in {language} for the {target_market} market. "
        f"Category: {category}. "
        f"Keywords: {', '.join(keywords)}. "
        f"Selling points: {'; '.join(selling_points)}. "
        f"Competitor ASINs to analyze: {asins_str}. "
        f"Use ISR competitor-detail-batch and keyword-value tools. "
        f"Apply FABE framework + Cosmo algorithm. "
        f"Self-evaluate quality and revise if < 90."
    )

    logger.info(f"ReAct: trying PCE A2A (listing-worker, timeout={A2A_TIMEOUT}s)")

    # === PCE A2A 优先（timeout 内完成则用 A2A，否则降级）===
    try:
        resp = requests.post(
            f"{PCE_A2A_BASE}/a2a/message:send",
            json={"agent": "listing-worker", "message": message},
            timeout=A2A_TIMEOUT,
        )

        if resp.status_code in (200, 202):
            result = _parse_a2a_sse(resp.text)
            if result:
                logger.info(f"A2A succeeded ({A2A_TIMEOUT}s timeout)")
                return result
        logger.info(f"A2A returned status={resp.status_code}")
    except requests.Timeout:
        logger.info(f"A2A timed out ({A2A_TIMEOUT}s), falling back")
    except Exception as e:
        logger.info(f"A2A failed ({e}), falling back")

    # === 降级到本地 ReAct ===
    return _react_local(
        product_name, category, keywords, selling_points,
        target_market, language, competitor_asins,
    )


def _react_local(
    product_name: str,
    category: str,
    keywords: list[str],
    selling_points: list[str],
    target_market: str,
    language: str,
    competitor_asins: list[str] | None,
) -> dict:
    """本地 ReAct 回退实现。

    当 PCE A2A react agent 不可用时，
    手动执行 Step 1(ISR 竞品) → Step 2(ISR 关键词) → Step 3-4(LLM 生成)。
    """
    competitor_data = {"competitors": {}, "keywords": []}

    # Step 1-2: ISR 竞品数据获取
    if competitor_asins:
        try:
            detail_resp = requests.post(
                f"{ISR_BASE}/search/competitor-detail-batch",
                json={"asins": competitor_asins},
                timeout=15,
            )
            if detail_resp.ok:
                competitor_data["competitors"] = detail_resp.json().get("competitors", {})

            kw_resp = requests.post(
                f"{ISR_BASE}/search/keyword-value",
                json={"asins": competitor_asins},
                timeout=15,
            )
            if kw_resp.ok:
                competitor_data["keywords"] = kw_resp.json().get("keywords", [])
        except Exception:
            logger.warning("ISR competitor fetch failed, continuing without")

    # Step 3-4: LLM 生成（复用 listing_generator 的 PCE 调用）
    result = generate_listing(
        product_name=product_name,
        category=category,
        keywords=keywords,
        selling_points=selling_points,
        target_market=target_market,
        language=language,
        competitor_data=competitor_data,
    )

    return result


def _parse_a2a_sse(sse_text: str) -> dict | None:
    """解析 PCE A2A SSE 流式响应，提取最终完成的 listing 内容。

    A2A 返回格式示例：
        data: {"status":"accepted",...}
        data: {"status":"completed","result":{"output":"**Title:**...","iterations":3,...}}
        data: [DONE]

    Returns:
        dict | None: { title, bullets, description, search_terms } 或 None
    """
    import re as _re

    completed_data = None
    for line in sse_text.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and "[DONE]" not in line:
            try:
                event = json.loads(line[6:])
                if event.get("status") == "completed":
                    completed_data = event.get("result", {})
                    break
            except json.JSONDecodeError:
                continue

    if not completed_data:
        return None

    output = completed_data.get("output", "")
    if not output:
        return None

    # 从 output 中解析标题、五点、描述
    title = ""
    bullets = []
    description = ""
    in_bullets = False

    for line in output.split("\n"):
        stripped = line.strip()
        # 标题：**Title:** ... 或 # Title
        if _re.match(r"^(\*\*Title|# Title|## Title)[\**:]", stripped):
            title = _re.sub(r"^(\*\*Title|# Title|## Title)[\*:]*\s*", "", stripped, flags=_re.IGNORECASE)
            title = _re.sub(r"\*\*", "", title)
        # 五点：- **...** 或 - ✅ ...
        elif _re.match(r"^-.*(✅|\*\*)", stripped):
            bullet = _re.sub(r"^- [✅*]*\*{0,2}", "", stripped)
            bullet = _re.sub(r"\*\*", "", bullet).strip()
            if bullet:
                bullets.append(bullet)
        # 描述段落（不在五点块中）
        elif stripped and not stripped.startswith("-") and ":**" not in stripped:
            if not in_bullets and len(stripped) > 30:
                description += stripped + " "

    # 从 A2A output 的 raw text 中尝试提取结构化数据
    result = _try_parse_json(output)
    if result:
        return result

    return {
        "title": title,
        "bullets": bullets[:5],
        "description": description.strip(),
        "search_terms": [],
    }


def _try_parse_json(content: str) -> dict | None:
    """尝试从 content 中提取 JSON 数据。"""
    if not content:
        return None

    # 直接解析
    content = content.strip()
    if content.startswith("{"):
        try:
            data = json.loads(content)
            return {
                "title": data.get("title", ""),
                "bullets": data.get("bullets", []),
                "description": data.get("description", ""),
                "search_terms": data.get("search_terms", []),
            }
        except json.JSONDecodeError:
            pass

    # 从 ```json 块中提取
    if "```json" in content:
        start = content.index("```json") + 7
        end = content.index("```", start) if "```" in content[start:] else len(content)
        try:
            data = json.loads(content[start:end].strip())
            return {
                "title": data.get("title", ""),
                "bullets": data.get("bullets", []),
                "description": data.get("description", ""),
                "search_terms": data.get("search_terms", []),
            }
        except (json.JSONDecodeError, ValueError):
            pass

    return None
