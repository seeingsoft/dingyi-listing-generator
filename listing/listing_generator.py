"""Listing 生成器 — Prompt 工程 + PCE /call 调用。

通过 PCE /api/v1/llm/call 端点，使用 listing-generation 标签（→ deepseek-v4-flash）
生成 Amazon listing 文案：标题、五点描述、产品描述、搜索词。
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import requests


# === PCE 配置 ===
PCE_API_BASE = os.environ.get("PCE_API_BASE", "http://127.0.0.1:8180")
PCE_CALL_ENDPOINT = f"{PCE_API_BASE}/api/v1/llm/call"

# 已知就绪标签（PCE STATUS.md 确认）
LISTING_TAG = "listing-generation"
LISTING_MODEL_HINT = "flash"  # listing-generation → deepseek-v4-flash

# ISR API
ISR_BASE = os.environ.get("ISR_API_BASE", "http://127.0.0.1:5000/api/v1")


class LLMError(Exception):
    """LLM 调用错误。"""
    pass


def _fetch_competitor_data(asins: list[str]) -> dict:
    """从 ISR 获取竞品详情 + 关键词价值。
    
    Args:
        asins: Amazon ASIN 列表
        
    Returns:
        dict: {"competitors": {...}, "keywords": [...]}
    """
    result: dict = {"competitors": {}, "keywords": []}
    if not asins:
        return result

    try:
        # 1. 竞品详情
        detail_resp = requests.post(
            f"{ISR_BASE}/search/competitor-detail-batch",
            json={"asins": asins},
            timeout=30,
        )
        if detail_resp.ok:
            result["competitors"] = detail_resp.json().get("competitors", {})

        # 2. 关键词价值
        kw_resp = requests.post(
            f"{ISR_BASE}/search/keyword-value",
            json={"asins": asins},
            timeout=30,
        )
        if kw_resp.ok:
            result["keywords"] = kw_resp.json().get("keywords", [])
    except requests.RequestException as e:
        # 竞品数据获取失败不阻塞 listing 生成
        pass

    return result


@dataclass
class ListingResult:
    title: str = ""
    bullets: list[str] = field(default_factory=list)
    description: str = ""
    search_terms: list[str] = field(default_factory=list)
    raw_response: str = ""


def generate_listing(
    product_name: str,
    category: str,
    keywords: list[str],
    selling_points: list[str],
    target_market: str = "US",
    language: str = "en",
    temperature: float = 0.1,
    competitor_data: dict | None = None,
) -> dict:
    """生成 Amazon listing 文案。

    Args:
        product_name: 产品名称
        category: Amazon 类目（如 "Sports & Outdoors > Water Bottles"）
        keywords: 搜索关键词列表
        selling_points: 核心卖点列表
        target_market: 目标市场（US/UK/DE/JP 等）
        language: 输出语言（en/de/ja 等）
        temperature: LLM 温度参数
        competitor_data: 竞品数据（来自 _fetch_competitor_data）

    Returns:
        dict: {"title": "...", "bullets": [...], "description": "...", "search_terms": [...]}
    """
    system_prompt = _build_system_prompt(target_market, language)
    user_prompt = _build_user_prompt(
        product_name, category, keywords, selling_points,
        target_market, language, competitor_data,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    return _call_pce_llm(messages, temperature)


def _build_lang_specific_rules(lang: str) -> str:
    """根据目标语言添加特定的 Listing 规则。"""
    rules = {
        "en": "",
        "ja": """
For Japanese (ja) marketplace:
- Title: max 500 full-width characters
- Bullets: max 500 full-width characters each
- Use formal Japanese (です/ます調)
- Include Japanese-specific keywords (カタカナ + 漢字 mixed)
- Follow Amazon.co.jp listing conventions""",
        "de": """
For German (de) marketplace:
- Title: max 200 characters
- Bullets: max 500 characters each
- Use formal German (Sie form, not du)
- Include German-specific keywords
- Follow Amazon.de listing conventions
- Mention CE certification if applicable""",
        "fr": """
For French (fr) marketplace:
- Title: max 200 characters
- Bullets: max 500 characters each
- Use formal French (vous form, not tu)
- Include French-specific keywords
- Follow Amazon.fr listing conventions""",
    }
    return rules.get(lang, "")


def _build_system_prompt(market: str, lang: str) -> str:
    """构建系统提示词（精简版 + FABE+Cosmo + 语言特定规则）。"""
    base = f"""You are an expert Amazon listing copywriter. Generate a listing in {lang} for {market}.

Rules:
1. Title: <=200 chars, include Brand+Product+Feature+Size+Material. Capitalize major words.
   No promotions ("Best Seller"), no prices, no special chars except - and &
2. Bullets (5): <=500 chars each. Start with capitalized feature heading.
   Focus on BENEFITS. No HTML, no all-caps
3. Description: <=2000 chars, paragraph form. Include use cases + specs
4. Search Terms: 5-10 lowercase terms, comma-separated. No duplicates from title"""

    fabe_cosmo = """
5. FABE Marketing Framework (apply to every bullet point):
   - Feature: Describe physical characteristics (e.g., "double-wall vacuum insulation, 304 stainless steel")
   - Advantage: Explain why better than alternatives (e.g., "keeps drinks cold for 34 hours, hot for 10 hours")
   - Benefit: Describe positive impact on user's life (e.g., "enjoy refreshing iced coffee from morning commute to evening workout")
   - Evidence: Provide proof or data (e.g., "100% leakproof tested seal, BPA-free certified")
   Each bullet point MUST contain a complete F-A-B-E chain.

6. Amazon Cosmo Algorithm Guidelines:
   - Emphasize context relevance and user intent matching
   - NO keyword stuffing — use natural language that matches buyer search intent
   - Place high-value keywords naturally in the first 80 characters of title
   - Each bullet should address a specific customer need (hydration, portability, durability, cleaning, versatility)
   - Use semantic keyword variations rather than exact-match repetition
   - Match the tone and terminology of Amazon's category best practices

Output ONLY valid JSON: {{"title":"...","bullets":["...",...],"description":"...","search_terms":["..."]}}"""

    lang_rules = _build_lang_specific_rules(lang)
    if lang_rules:
        return base + "\n" + fabe_cosmo + "\n" + lang_rules
    return base + "\n" + fabe_cosmo


def _build_user_prompt(
    product_name: str,
    category: str,
    keywords: list[str],
    selling_points: list[str],
    market: str,
    lang: str,
    competitor_data: dict | None = None,
) -> str:
    """构建用户提示词。"""
    kw_str = ", ".join(keywords)
    sp_str = "\n".join(f"  - {sp}" for sp in selling_points)

    # 竞品数据注入
    competitor_section = ""
    if competitor_data and competitor_data.get("competitors"):
        comps = competitor_data["competitors"]
        comp_lines = []
        for asin, info in list(comps.items())[:3]:  # 最多 3 个竞品
            title = info.get("title", "")[:100]
            price = info.get("price", "N/A")
            rating = info.get("rating", "N/A")
            comp_lines.append(f"- {asin}: {title} (${price}, ★{rating})")
        if comp_lines:
            competitor_section = "\n**Competitor Analysis**:\n" + "\n".join(comp_lines)

    keyword_section = ""
    if competitor_data and competitor_data.get("keywords"):
        kws = competitor_data["keywords"]
        top_kws = [kw.get("keyword", "") for kw in kws[:10] if kw.get("keyword")]
        if top_kws:
            keyword_section = "\n**High-Value Keywords from Competitors**: " + ", ".join(top_kws)

    return f"""Generate an Amazon listing for the following product:

**Product**: {product_name}
**Category**: {category}
**Target Keywords**: {kw_str}
**Selling Points**:
{sp_str}
**Market**: {market}
**Language**: {lang}{competitor_section}{keyword_section}

Please generate:
1. A compelling title (<200 chars)
2. Five benefit-focused bullet points
3. A descriptive product description
4. Backend search terms (as an array)

Return ONLY a JSON object with the following schema:

```json
{{
  "title": "string (max 200 chars)",
  "bullets": ["bullet1", "bullet2", "bullet3", "bullet4", "bullet5"],
  "description": "string (max 2000 chars)",
  "search_terms": ["term1", "term2", "term3", "term4", "term5", "term6", "term7", "term8"]
}}
```"""


def _call_pce_llm(messages: list[dict], temperature: float = 0.1) -> dict:
    """调用 PCE /api/v1/llm/call。

    标签: listing-generation → deepseek-v4-flash
    请求体格式与 PCE SDK 保持一致。
    """
    body = {
        "tag": LISTING_TAG,
        "model_hint": LISTING_MODEL_HINT,
        "messages": messages,
        "options": {
            "temperature": temperature,
            "max_tokens": 4000,
            "timeout_ms": 30000,
        },
    }

    req = Request(
        PCE_CALL_ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    for attempt in range(3):
        try:
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if not data.get("success"):
                raise LLMError(f"PCE call failed: {data.get('error', 'unknown')}")

            content = data["data"].get("content", "")
            json_data = data["data"].get("json")

            # PCE 自动解析 JSON 响应
            if json_data:
                return {
                    "title": json_data.get("title", ""),
                    "bullets": json_data.get("bullets", []),
                    "description": json_data.get("description", ""),
                    "search_terms": json_data.get("search_terms", []),
                }

            # 回退：手动从 content 提取 JSON
            return _extract_json_from_content(content)

        except HTTPError as e:
            if e.code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:200]}")
        except URLError as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise LLMError(f"Connection error: {e.reason}")
        except (json.JSONDecodeError, KeyError) as e:
            raise LLMError(f"Response parse error: {e}")

    raise LLMError("Max retries exceeded")


def _extract_json_from_content(content: str) -> dict:
    """从 LLM 文本响应中提取 JSON。"""
    # 尝试找到 JSON 块
    if "```json" in content:
        start = content.index("```json") + 7
        end = content.index("```", start)
        content = content[start:end]
    elif "```" in content:
        start = content.index("```") + 3
        end = content.index("```", start)
        content = content[start:end]

    data = json.loads(content.strip())
    return {
        "title": data.get("title", ""),
        "bullets": data.get("bullets", []),
        "description": data.get("description", ""),
        "search_terms": data.get("search_terms", []),
    }


def health_check() -> dict:
    """检查 PCE 连接状态。"""
    try:
        return _call_pce_llm(
            messages=[{"role": "user", "content": "Reply with just 'ok' in JSON: {\"status\": \"ok\"}"}],
            temperature=0.0,
        )
    except LLMError as e:
        return {"status": "error", "error": str(e)}
