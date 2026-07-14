"""合规词库校验 — Amazon 禁售/限售/侵权词检测。

复用 ISR 品类知识库 + Amazon 禁售/限售词表。
"""

import re
from typing import NamedTuple


class ComplianceReport(NamedTuple):
    passed: bool
    violations: list[dict]  # [{"word": "...", "type": "prohibited"|"restricted"|"ip_risk", "location": "title"|"bullets"|"description"}]


def _word_matches(word: str, text: str) -> bool:
    """检查 word 是否在 text 中以完整单词形式出现。
    
    单字词用 \b 边界匹配（避免 "chemical" 匹配 "chemicals"），
    多字短语用子串匹配（支持 "pet food"、"baby crib"）。
    """
    if " " in word.strip():
        return word in text
    return bool(re.search(r'\b' + re.escape(word) + r'\b', text))


# === 禁售词（绝对不可出现） ===
PROHIBITED_WORDS: set[str] = {
    "firearm", "firearms", "gun", "guns", "rifle", "pistol", "ammunition",
    "weapon", "weapons", "knife", "knives", "dagger", "sword",
    "drug", "drugs", "narcotic", "cocaine", "heroin", "methamphetamine",
    "marijuana", "cannabis", "hemp oil", "cbd",
    "tobacco", "cigarette", "cigarettes", "vape", "vaping", "e-cigarette",
    "alcohol", "liquor", "beer", "wine", "whiskey", "vodka",
    "medication", "prescription", "pharmaceutical",
    "hazardous", "explosive", "explosives", "toxic", "poison", "poisonous",
    "pesticide", "radioactive",
    "lock pick", "lockpick",
    "surveillance", "spy camera", "hidden camera",
    "counterfeit", "fake", "replica", "knockoff",
    "human remains", "bodily fluids",
    "lottery", "gambling",
    "child", "minor", "pornographic", "pornography",
}

# === 限售词（需要特殊资质或分类审批） ===
RESTRICTED_WORDS: set[str] = {
    "supplement", "supplements", "dietary", "vitamin", "vitamins",
    "herbal", "herb", "remedy",
    "pet food", "dog food", "cat food",
    "battery", "batteries", "lithium", "li-ion", "lithium-ion",
    "chemical", "chemicals", "corrosive",
    "laser", "laser pointer",
    "medical device", "medical devices",
    "infant", "baby crib", "bassinet", "baby carrier",
    "car seat", "child seat", "booster seat",
}

# === 侵权风险词（品牌名需额外审查） ===
IP_RISK_WORDS: set[str] = {
    "nike", "adidas", "apple", "samsung",
    "disney", "marvel", "dc comics",
    "harry potter", "star wars", "pokemon",
    "lego", "barbie", "hot wheels",
    "hello kitty", "minions", "frozen",
    "avengers", "spider-man", "superman", "batman",
    "nintendo", "playstation", "xbox",
    "lululemon", "chanel", "gucci", "louis vuitton", "prada",
    "rolex", "cartier",
    "yeti", "hydro flask", "stanley cup",  # 热门品牌，需谨慎
}


def check_compliance(
    title: str,
    bullets: list[str],
    description: str,
    product_name: str = "",
) -> ComplianceReport:
    """检查生成的 listing 文案是否符合 Amazon 合规要求。

    Args:
        title: 生成的标题
        bullets: 生成的五点描述列表
        description: 生成的产品描述
        product_name: 产品名称（用于排除自身品牌名的误报）

    Returns:
        ComplianceReport: 合规检查结果
    """
    violations: list[dict] = []
    combined_text = f"{title.lower()} {' '.join(b.lower() for b in bullets)} {description.lower()}"

    # 移除否定形式（如 "non-toxic"、"non hazardous"），避免误报
    import re as _re
    combined_text = _re.sub(r'\bnon[-\s]*(toxic|hazardous|explosive|poisonous|radioactive|chemical)\b', '', combined_text)
    # 移除否定语境中的 chemical 词本身
    combined_text = _re.sub(r'\b(?:no|free from|without)\b.*?\bchemicals?\b', lambda m: m.group()[:-len(m.group().split()[-1])].strip(), combined_text)
    combined_text = _re.sub(r'\bchemical-free\b', '', combined_text)

    # 构建排除词集：产品名中的词不应被误报
    exclude_words: set[str] = set()
    if product_name:
        exclude_words = {w.lower() for w in product_name.split(" ") if len(w) > 2}

    # 1. 检查禁售词
    for word in PROHIBITED_WORDS:
        if _word_matches(word, combined_text):
            location = _locate_word(word, title, bullets, description)
            violations.append({"word": word, "type": "prohibited", "location": location})

    # 2. 检查限售词
    for word in RESTRICTED_WORDS:
        if _word_matches(word, combined_text):
            location = _locate_word(word, title, bullets, description)
            violations.append({"word": word, "type": "restricted", "location": location})

    # 3. 检查侵权风险词（排除产品名自身的词）
    for word in IP_RISK_WORDS:
        if _word_matches(word, combined_text) and word not in exclude_words:
            location = _locate_word(word, title, bullets, description)
            violations.append({"word": word, "type": "ip_risk", "location": location})

    return ComplianceReport(
        passed=len(violations) == 0,
        violations=violations,
    )


def _locate_word(word: str, title: str, bullets: list[str], description: str) -> str:
    """定位违规词出现在哪个字段中。"""
    title_lower = title.lower()
    if _word_matches(word, title_lower):
        return "title"
    for i, bullet in enumerate(bullets):
        if _word_matches(word, bullet.lower()):
            return f"bullet_{i + 1}"
    if _word_matches(word, description.lower()):
        return "description"
    return "unknown"


def get_quality_score(violations: list[dict]) -> int:
    """根据违规情况计算质量分数（0-100）。
    
    - 禁售词：每个 -30
    - 限售词：每个 -15  
    - 侵权风险词：每个 -10
    - 品牌引用：每个 -2
    """
    base_score = 100
    for v in violations:
        if v["type"] == "prohibited":
            base_score -= 30
        elif v["type"] == "restricted":
            base_score -= 15
        elif v["type"] == "ip_risk":
            base_score -= 10
        elif v["type"] == "brand_reference":
            base_score -= 2
    return max(0, base_score)


# === 竞品品牌词库（40 oz Tumbler 品类常见品牌） ===
COMPETITOR_BRANDS = [
    'stanley', 'yeti', 'owala', 'brumate', 'hydroflask', 'ello',
    'simple modern', 'contigo', 'camelbak', 'kleen kanteen', 's well',
    'reducer', 'bubba', 'zak', 'tervis', 'rtic', 'corkcicle',
]


def check_brand_filter(title: str, bullets: list[str], description: str) -> list[dict]:
    """检查 Listing 中是否混入竞品品牌词。返回 violations。"""
    violations = []
    full_text = f"{title} {' '.join(bullets)} {description}".lower()

    for brand in COMPETITOR_BRANDS:
        if _word_matches(brand, full_text):
            violations.append({
                "type": "brand_reference",
                "word": brand,
                "severity": "warning",
                "location": "unknown",
                "message": f"竞品品牌词 '{brand}' 不应出现在文案中"
            })
    return violations
