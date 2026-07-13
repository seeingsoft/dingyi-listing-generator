"""合规前置筛查 API — 禁用词/侵权/认证缺失检测。

输入 Listing 内容，返回合规报告（含建议和置信度）。
"""

import traceback
from typing import Any

from compliance_checker import (
    check_compliance,
    check_brand_filter,
    COMPETITOR_BRANDS,
    get_quality_score,
)
from compliance_rules import (
    CATEGORY_CERTIFICATIONS,
    get_required_certifications,
    check_listing_content_for_cert,
)


def check_compliance_full(
    title: str,
    bullets: list[str],
    description: str,
    category: str,
    target_market: str,
    product_name: str = "",
) -> dict[str, Any]:
    """完整的合规前置检查。

    Args:
        title: Listing 标题
        bullets: 五点描述列表
        description: 产品描述
        category: Amazon 品类路径
        target_market: 目标市场代码
        product_name: 产品名称（用于排除自身品牌误报）

    Returns:
        dict: {
            "passed": bool,
            "violations": list[dict],
            "suggestions": list[str],
            "confidence": float,
            "summarized": {禁售词: N, 限售词: N, 侵权/品牌: N, 认证缺失: N}
        }
    """
    violations: list[dict] = []
    suggestions: list[str] = []

    combined = f"{title} {' '.join(bullets)} {description}"

    # 1. 禁用词检测（复用 check_compliance）
    base_report = check_compliance(
        title=title,
        bullets=bullets,
        description=description,
        product_name=product_name,
    )
    violations.extend(base_report.violations)

    for v in base_report.violations:
        if v["type"] == "prohibited":
            suggestions.append(f"移除禁用词「{v['word']}」（位于 {v['location']}）")
        elif v["type"] == "restricted":
            suggestions.append(f"注意限售词「{v['word']}」（位于 {v['location']}），可能需特殊资质")
        elif v["type"] == "ip_risk":
            suggestions.append(f"注意侵权风险词「{v['word']}」（位于 {v['location']}），建议删除或替换")

    # 2. 侵权/品牌词检测（复用 check_brand_filter）
    brand_v = check_brand_filter(
        title=title,
        bullets=bullets,
        description=description,
    )
    violations.extend(brand_v)

    for v in brand_v:
        suggestions.append(f"移除竞品品牌词「{v['word']}」——避免侵权投诉")

    # 3. 认证缺失检测
    required_certs = get_required_certifications(category, target_market)
    missing_certs = check_listing_content_for_cert(combined, required_certs)
    for cert in missing_certs:
        violations.append({
            "type": "认证缺失",
            "certification": cert,
            "severity": "medium",
            "category": category.split(" > ")[0] if " > " in category else category,
            "location": "listing",
            "word": "",
        })
        suggestions.append(f"补充 {cert} 认证信息（{category} → {target_market} 市场要求）")

    # 4. 计算置信度
    # 禁用词/限售词检测基于精确词表 → 高置信度
    # 品牌过滤基于词表 → 中置信度
    # 认证缺失基于规则表 → 中置信度
    has_prohibited = any(v["type"] == "prohibited" for v in violations)
    total_violations = len(violations)
    if total_violations == 0:
        confidence = 0.95
    elif has_prohibited:
        confidence = 0.99  # 禁售词检出率高置信度
    else:
        confidence = round(0.90 - 0.02 * total_violations, 2)
        confidence = max(0.50, confidence)

    # 5. 汇总统计
    summarized = {
        "禁售词": sum(1 for v in violations if v["type"] == "prohibited"),
        "限售词": sum(1 for v in violations if v["type"] == "restricted"),
        "侵权/品牌": sum(1 for v in violations if v["type"] in ("ip_risk", "brand_reference")),
        "认证缺失": sum(1 for v in violations if v["type"] == "认证缺失"),
    }

    # 6. 整体通过判定
    # 禁售词 → 强制不通过；限售词+侵权+认证缺失 → 警告，可通过
    passed = not has_prohibited

    return {
        "passed": passed,
        "violations": violations,
        "suggestions": suggestions,
        "confidence": confidence,
        "summarized": summarized,
    }
