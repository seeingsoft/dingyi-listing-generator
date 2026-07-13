"""跨平台发布适配器 — Amazon + Walmart 格式适配。

定义 PlatformAdapter 接口和首批平台实现，
负责按各平台规则格式化 Listing 内容并通过校验。
"""

import abc
import re
from typing import Any


# === 平台适配器接口 ===

class PlatformAdapter(abc.ABC):
    """平台适配器抽象基类。"""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """平台名称标识。"""
        ...

    @abc.abstractmethod
    def format_title(self, title: str) -> str:
        """按平台限制格式化标题。"""
        ...

    @abc.abstractmethod
    def format_bullets(self, bullets: list[str]) -> list[str]:
        """按平台要求格式化五点描述。"""
        ...

    @abc.abstractmethod
    def format_description(self, description: str) -> str:
        """格式化产品描述。"""
        ...

    @abc.abstractmethod
    def validate(self, listing: dict) -> list[str]:
        """平台特定规则校验。返回 warnings 列表（空 = 通过）。"""
        ...

    def format_keywords(self, keywords: list[str]) -> str:
        """格式化搜索关键词（默认用空格分隔）。"""
        return " ".join(keywords)


# === Amazon 适配器 ===

class AmazonAdapter(PlatformAdapter):
    """Amazon 平台适配器。

    规则：
    - 标题 ≤ 200 字符
    - 五点描述每点 ≤ 500 字符
    - 搜索关键词合计 ≤ 250 字节
    - 无 HTML 标签
    """

    @property
    def name(self) -> str:
        return "amazon"

    def format_title(self, title: str) -> str:
        cleaned = _strip_html(title).strip()
        if len(cleaned) > 200:
            cleaned = cleaned[:197] + "..."
        return cleaned

    def format_bullets(self, bullets: list[str]) -> list[str]:
        result = []
        for b in bullets:
            cleaned = _strip_html(b).strip()
            if len(cleaned) > 500:
                cleaned = cleaned[:497] + "..."
            result.append(cleaned)
        return result

    def format_description(self, description: str) -> str:
        return _strip_html(description).strip()

    def format_keywords(self, keywords: list[str]) -> str:
        # Amazon 关键词用空格分隔，合计 ≤ 250 字节
        joined = " ".join(keywords)
        while len(joined.encode("utf-8")) > 250 and joined:
            joined = " ".join(joined.split()[:-1])
        return joined

    def validate(self, listing: dict) -> list[str]:
        warnings = []
        title = listing.get("title", "")
        bullets = listing.get("bullets", [])
        description = listing.get("description", "")
        keywords = listing.get("keywords", [])

        if len(title) > 200:
            warnings.append(f"标题超出 200 字符限制（当前 {len(title)} 字符）")
        for i, b in enumerate(bullets):
            if len(b) > 500:
                warnings.append(f"Bullet {i+1} 超出 500 字符限制（当前 {len(b)} 字符）")
        kw_str = " ".join(keywords)
        if len(kw_str.encode("utf-8")) > 250:
            warnings.append(f"搜索关键词超出 250 字节限制")
        if _has_html(title) or _has_html(description):
            warnings.append("Listing 内容含 HTML 标签，请移除")

        return warnings


# === Walmart 适配器 ===

class WalmartAdapter(PlatformAdapter):
    """Walmart 平台适配器。

    规则：
    - 标题 ≤ 200 字符
    - 描述 ≤ 4000 字符
    - 图片 ≥ 4 张（待扩展）
    - 无 HTML 标签
    """

    @property
    def name(self) -> str:
        return "walmart"

    def format_title(self, title: str) -> str:
        cleaned = _strip_html(title).strip()
        if len(cleaned) > 200:
            cleaned = cleaned[:197] + "..."
        return cleaned

    def format_bullets(self, bullets: list[str]) -> list[str]:
        # Walmart 标题式要点（更简洁）
        result = []
        for b in bullets:
            cleaned = _strip_html(b).strip()
            # 取第一句（按句号或分号分割）
            first_sentence = re.split(r"[.;]", cleaned)[0].strip()
            if len(first_sentence) > 300:
                first_sentence = first_sentence[:297] + "..."
            result.append(first_sentence)
        return result

    def format_description(self, description: str) -> str:
        cleaned = _strip_html(description).strip()
        if len(cleaned) > 4000:
            cleaned = cleaned[:3997] + "..."
        return cleaned

    def validate(self, listing: dict) -> list[str]:
        warnings = []
        title = listing.get("title", "")
        description = listing.get("description", "")
        bullets = listing.get("bullets", [])

        if len(title) > 200:
            warnings.append(f"标题超出 200 字符限制（当前 {len(title)} 字符）")
        desc_len = len(description)
        if desc_len > 4000:
            warnings.append(f"描述超出 4000 字符限制（当前 {desc_len} 字符）")
        if len(bullets) < 4:
            warnings.append(f"建议至少 4 张图片（当前 {len(bullets)} 个描述点）")
        if _has_html(title) or _has_html(description):
            warnings.append("Listing 内容含 HTML 标签，请移除")

        return warnings


# === 注册表 ===

_ADAPTERS: dict[str, PlatformAdapter] = {}

def _register(adapter: PlatformAdapter) -> None:
    _ADAPTERS[adapter.name] = adapter

_register(AmazonAdapter())
_register(WalmartAdapter())


def get_adapter(platform: str) -> PlatformAdapter | None:
    """获取指定平台的适配器。"""
    return _ADAPTERS.get(platform)


def list_platforms() -> list[str]:
    """返回所有已注册平台。"""
    return list(_ADAPTERS.keys())


def format_for_platforms(
    listing: dict,
    platforms: list[str],
) -> dict[str, Any]:
    """为多个平台格式化 Listing。

    Args:
        listing: { title, bullets, description, keywords }
        platforms: 平台名称列表 ["amazon", "walmart"]

    Returns:
        dict: { "amazon": {..}, "walmart": {..} }
    """
    results = {}
    for p in platforms:
        adapter = get_adapter(p)
        if not adapter:
            results[p] = {"error": f"不支持的平台: {p}"}
            continue

        formatted = {
            "title": adapter.format_title(listing.get("title", "")),
            "bullets": adapter.format_bullets(listing.get("bullets", [])),
            "description": adapter.format_description(listing.get("description", "")),
            "search_terms": adapter.format_keywords(listing.get("keywords", [])),
            "validation_passed": True,
            "warnings": [],
        }
        formatted["warnings"] = adapter.validate(_build_listing_dict(formatted))
        formatted["validation_passed"] = len(formatted["warnings"]) == 0
        results[p] = formatted

    return results


def _build_listing_dict(formatted: dict) -> dict:
    """从格式化结果重建 listing dict 用于校验。"""
    return {
        "title": formatted["title"],
        "bullets": formatted["bullets"],
        "description": formatted["description"],
        "keywords": formatted["search_terms"].split() if isinstance(formatted.get("search_terms"), str) else [],
    }


def _strip_html(text: str) -> str:
    """移除 HTML 标签。"""
    return re.sub(r"<[^>]+>", "", text) if text else ""


def _has_html(text: str) -> bool:
    """检查是否含 HTML 标签。"""
    return bool(re.search(r"<[^>]+>", text)) if text else False
