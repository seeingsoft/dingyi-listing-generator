"""爆款横向裂变生成器 — 从单品 Listing 生成差异化变体。

支持颜色/尺寸/材质/功能/自动 五种裂变模式。
"""

import json
import logging
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from typing import Any

logger = logging.getLogger("listing-variant")

PCE_API_BASE = os.environ.get("PCE_API_BASE", "http://127.0.0.1:8180")

# 各裂变维度的示例关键词，用于 prompt 引导
DIMENSION_GUIDE = {
    "color": {
        "label": "颜色",
        "description": "按颜色/配色裂变，每种变体使用不同颜色作为核心差异化特征",
        "examples": "Silver → Black, White, Rose Gold",
        "instruction": "为每个变体选择一种鲜明不同的颜色，标题中包含颜色词，五点描述中强调颜色相关的卖点",
    },
    "size": {
        "label": "尺寸/容量",
        "description": "按尺寸/容量裂变，每种变体使用不同规格",
        "examples": "500ml → 350ml, 750ml, 1000ml",
        "instruction": "为每个变体分配不同容量/尺寸，五点描述中说明尺寸差异带来的使用场景变化（如便携 vs 家庭装）",
    },
    "material": {
        "label": "材质",
        "description": "按材质裂变，每种变体使用不同材料",
        "examples": "Stainless Steel → Titanium, Glass, Tritan",
        "instruction": "为每个变体选择不同材质，强调各自材质的独特优势（如轻量/耐用/环保）",
    },
    "feature": {
        "label": "功能",
        "description": "按功能定位裂变，每种变体面向不同使用场景",
        "examples": "基础款 → 智能温显款, 运动便携款, 商务礼品款",
        "instruction": "为每个变体设计不同的功能定位和目标场景，五点描述中突出功能差异",
    },
}

SYSTEM_PROMPT = """你是一个 Amazon 产品变体专家。分析源 Listing，按照指定维度生成差异化变体。

每个变体需要：
1. 标题：融入差异化特征词，保持 ≤200 字符
2. 五点描述（4-5 条）：保留核心卖点 + 增加差异化卖点
3. 产品描述：强调变体独特优势
4. differentiation：一句简短说明该变体与源产品的核心差异

保持与源Listing一致的品牌调性和格式规范。
使用英语输出。

返回严格符合以下 JSON 格式（不要包含任何其他文字）：

```json
{
  "variants": [
    {
      "title": "差异化标题",
      "bullets": ["第1点", "第2点", "第3点", "第4点", "第5点"],
      "description": "产品描述",
      "differentiation": "与源产品的核心差异",
      "variant_tag": "变体标识（如 Black / 750ml / Titanium）"
    }
  ],
  "analysis": "裂变维度选择理由说明"
}
```"""


def generate_variants(
    source_title: str,
    source_bullets: list[str],
    source_description: str,
    category: str,
    split_dimension: str = "auto",
    variant_count: int = 3,
) -> dict[str, Any]:
    """生成裂变变体。

    Args:
        source_title: 源标题
        source_bullets: 源五点描述
        source_description: 源产品描述
        category: Amazon 品类路径
        split_dimension: 裂变维度（color/size/material/feature/auto）
        variant_count: 变体数量（默认 3）

    Returns:
        dict: { variants: [...], analysis: "...", quality_score: int }
    """
    start = time.time()

    if split_dimension == "auto":
        split_dimension_used = "auto"
        dim_guide = "请智能分析该品类和产品特征，选择最优的裂变维度（颜色/尺寸/材质/功能）。"
    else:
        split_dimension_used = split_dimension
        guide = DIMENSION_GUIDE.get(split_dimension, DIMENSION_GUIDE["color"])
        dim_guide = f"裂变维度：{guide['label']}\n{guide['description']}\n示例：{guide['examples']}\n{guide['instruction']}"

    user_prompt = f"""请分析以下源 Listing，按指定维度裂变生成 {variant_count} 个差异化变体。

## 源 Listing
**标题**: {source_title}
**品类**: {category}
**五点描述**:
{chr(10).join(f'{i+1}. {b}' for i, b in enumerate(source_bullets))}
**产品描述**: {source_description[:300]}

## 裂变要求
{dim_guide}
变体数量：{variant_count}

## 输出要求
- 每个变体的标题 ≤ 200 字符
- 每个变体 4-5 条五点描述
- 保持品牌调性一致
- 变体之间要有实质性差异（不仅仅是换词）"""

    body = {
        "tag": "listing-generation",
        "model_hint": "pro",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }

    logger.info(f"Generating {variant_count} variants for '{source_title[:40]}' ({split_dimension_used})")

    try:
        req = Request(
            f"{PCE_API_BASE}/api/v1/llm/call",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=60) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError) as e:
        if isinstance(e, HTTPError):
            error_body = e.read().decode("utf-8", errors="ignore")[:200]
            logger.error(f"PCE LLM HTTP {e.code}: {error_body}")
        else:
            logger.error(f"PCE LLM connection error: {e.reason}")
        raise

    content = response.get("data", {}).get("content", "") or response.get("content", "")

    if not content:
        raise RuntimeError("PCE LLM returned empty content")

    result = _parse_variants(content, split_dimension_used, source_title)

    elapsed = time.time() - start
    logger.info(f"Generated {len(result['variants'])} variants in {elapsed:.1f}s, quality={result['quality_score']}")

    return result


def _parse_variants(content: str, split_dimension: str, source_title: str) -> dict:
    """解析 LLM 响应中的变体数据。"""
    # 提取 JSON
    content = content.strip()
    for marker in ["```json", "```"]:
        if marker in content:
            start = content.index(marker) + len(marker)
            end = content.index("```", start) if "```" in content[start:] else len(content)
            content = content[start:end].strip()
            break

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 尝试修复
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            data = json.loads(content[start:end])
        except (ValueError, json.JSONDecodeError):
            raise RuntimeError(f"Cannot parse LLM response: {content[:200]}")

    raw_variants = data.get("variants", [])
    if not raw_variants:
        raise RuntimeError("LLM returned no variants")

    variants = []
    for v in raw_variants:
        bullets = v.get("bullets", [])
        if isinstance(bullets, str):
            bullets = [bullets]
        variants.append({
            "title": v.get("title", "")[:200],
            "bullets": bullets[:5],
            "description": v.get("description", ""),
            "differentiation": v.get("differentiation", ""),
            "variant_tag": v.get("variant_tag", ""),
        })

    # 计算质量分
    quality = _score_variants(variants, split_dimension)

    return {
        "source": source_title,
        "split_dimension": split_dimension,
        "variants": variants,
        "analysis": data.get("analysis", ""),
        "quality_score": quality,
    }


def _score_variants(variants: list[dict], dimension: str) -> int:
    """评估变体质量。"""
    if not variants:
        return 0

    score = 85  # 基础分

    # 标题包含差异化关键词
    dim_keywords = {"color": ["black", "white", "gold", "blue", "red", "pink", "rose", "silver", "gray", "green"],
                    "size": ["ml", "oz", "gallon", "pack", "ounce", "liter", "750", "350", "1000", "500"],
                    "material": ["titanium", "glass", "tritan", "plastic", "ceramic", "aluminum", "copper"],
                    "feature": ["smart", "pro", "premium", "lite", "basic", "plus", "straw", "sport", "travel"]}

    # auto 模式合并所有维度关键词
    if dimension == "auto":
        kws = list(set(sum(dim_keywords.values(), [])))
    else:
        kws = dim_keywords.get(dimension, dim_keywords.get("color", []))

    found = 0
    for v in variants:
        title_lower = v.get("title", "").lower()
        for kw in kws:
            if kw in title_lower:
                found += 1
                break
    if found >= len(variants):
        score += 3  # 全部含差异化词加分
    else:
        score -= max(0, (len(variants) - found) * 3)  # 减轻扣分

    # 五点完整性
    for v in variants:
        if len(v.get("bullets", [])) < 4:
            score -= 5
        if len(v.get("bullets", [])) < 3:
            score -= 10

    # differentiation 完整性
    for v in variants:
        if not v.get("differentiation"):
            score -= 3

    return max(0, min(95, score))
