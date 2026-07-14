"""合规认证规则表 — 基于品类 + 目标市场的必备认证映射。

用于合规前置筛查端点，判断 Listing 是否缺少必要认证信息。
"""

# === 品类-市场 认证映射 ===
# 格式：{ "品类路径": { "市场代码": ["认证1", "认证2"], ... }, ... }
CATEGORY_CERTIFICATIONS: dict[str, dict[str, list[str]]] = {
    # === 电子产品 ===
    "Electronics": {
        "US": ["FCC"],
        "EU": ["CE"],
        "UK": ["UKCA"],
        "JP": ["PSE"],
        "CN": ["CCC"],
        "AU": ["RCM"],
        "CA": ["ICES"],
    },
    "Electronics > Computers & Accessories": {
        "US": ["FCC", "UL"],
        "EU": ["CE", "WEEE"],
    },
    "Electronics > Headphones": {
        "US": ["FCC"],
        "EU": ["CE"],
    },

    # === 玩具 ===
    "Toys": {
        "US": ["CPSC", "ASTM F963"],
        "EU": ["CE", "EN71"],
        "UK": ["UKCA", "EN71"],
        "JP": ["ST 2016"],
        "AU": ["AS/NZS 8124"],
        "CA": ["CCPSA", "SOR/2011-17"],
    },
    "Toys > Baby & Toddler Toys": {
        "US": ["CPSC", "ASTM F963"],
        "EU": ["CE", "EN71", "EN 62115"],
    },

    # === 母婴 ===
    "Baby Products": {
        "US": ["CPSC", "ASTM F963"],
        "EU": ["CE", "EN 71"],
    },
    "Baby Products > Baby Feeding": {
        "US": ["FDA", "BPA-free"],
        "EU": ["CE", "EN 14350"],
    },
    "Baby Products > Car Seats & Accessories": {
        "US": ["NHTSA", "FMVSS 213"],
        "EU": ["ECE R44/04", "ECE R129"],
    },

    # === 食品接触 ===
    "Kitchen & Dining > Food & Beverage Containers": {
        "US": ["FDA", "BPA-free"],
        "EU": ["EU 10/2011", "CE"],
        "JP": ["JHOSPA"],
        "CN": ["GB 4806"],
    },
    "Sports & Outdoors > Water Bottles": {
        "US": ["FDA", "BPA-free"],
        "EU": ["CE", "EU 10/2011"],
        "JP": ["JHOSPA"],
    },

    # === 服装配饰 ===
    "Clothing": {
        "US": ["CPSIA"],
        "EU": ["CE", "REACH"],
        "UK": ["UKCA"],
    },
    "Clothing > Kids & Baby": {
        "US": ["CPSIA", "ASTM F963"],
        "EU": ["CE", "EN 14682"],
    },

    # === 美容个护 ===
    "Beauty & Personal Care": {
        "US": ["FDA"],
        "EU": ["CE", "EU 1223/2009"],
        "UK": ["UKCA"],
    },

    # === 家居 ===
    "Home & Kitchen > Furniture": {
        "US": ["CPSC", "ASTM F2057"],
    },

    # === 运动户外 ===
    "Sports & Outdoors": {
        "US": ["CPSC"],
        "EU": ["CE"],
    },
}


def get_required_certifications(category: str, target_market: str) -> list[str]:
    """根据品类路径 + 目标市场，返回所需认证列表。

    Args:
        category: 品类路径（如 "Sports & Outdoors > Water Bottles"）
        target_market: 目标市场代码（US/EU/UK/JP 等）

    Returns:
        list[str]: 所需认证列表（如 ["FDA", "BPA-free"]）
    """
    # 先尝试精确匹配
    # 逐级回退（Water Bottles → Sports & Outdoors → 通用）
    if category in CATEGORY_CERTIFICATIONS:
        certs = CATEGORY_CERTIFICATIONS[category].get(target_market, [])
        if certs:
            return certs

    # 逐级回退：取 top-level 品类
    top_level = category.split(" > ")[0] if " > " in category else category
    if top_level in CATEGORY_CERTIFICATIONS:
        certs = CATEGORY_CERTIFICATIONS[top_level].get(target_market, [])
        if certs:
            return certs

    # 回退：US 默认需要 CPSC，EU 默认需要 CE
    defaults = {"US": ["CPSC"], "EU": ["CE"], "UK": ["UKCA"], "JP": [], "AU": [], "CA": []}
    return defaults.get(target_market, [])


def check_listing_content_for_cert(
    content: str,
    required_certs: list[str],
) -> list[str]:
    """检查 Listing 文案中是否已包含所需认证。

    Args:
        content: 合并后的 Listing 文案（title + bullets + description）
        required_certs: 所需认证列表

    Returns:
        list[str]: 缺少的认证名称
    """
    content_lower = content.lower()
    missing = []
    for cert in required_certs:
        cert_lower = cert.lower()
        # 直接匹配
        if cert_lower in content_lower:
            continue
        # 缩写匹配（如 "FCC" 可匹配 "FCC certified"）
        if cert_lower.replace(" ", "") in content_lower.replace(" ", ""):
            continue
        missing.append(cert)
    return missing
