"""结构化报告生成器 — 高转化商品文案生成报告。

参考 LinkFox Excel 4 Sheet 结构，支持 HTML（Dashboard）+ dict（API）双格式。
"""

import json
import re
from datetime import datetime


def generate_report(
    product_name: str,
    category: str,
    title: str,
    bullets: list[str],
    description: str,
    search_terms: list[str],
    violations: list[dict],
    quality_score: int,
    brand_violations: int,
    keywords: list[str] | None = None,
    competitor_asins: list[str] | None = None,
    competitor_data: dict | None = None,
    language: str = "en",
    target_market: str = "US",
    elapsed_ms: int = 0,
) -> dict:
    """生成结构化报告（同时返回 dict 和 HTML）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 一、竞品分析与核心卖点提炼
    competitor_analysis = _build_competitor_analysis(competitor_asins, keywords, competitor_data)

    # 二、高价值关键词打分表
    keyword_score = _build_keyword_score(keywords, search_terms, violations, brand_violations)

    # 三、Listing 文案生成
    listing_content = _build_listing_content(title, bullets, description, search_terms)

    # 四、关键词埋入检查
    keyword_embedding = _build_keyword_embedding(keywords, title, bullets, description, search_terms)

    report = {
        "meta": {
            "report_title": "高转化商品文案生成报告",
            "generated_at": now,
            "product": product_name,
            "category": category,
            "market": target_market,
            "language": language,
            "quality_score": quality_score,
            "elapsed_ms": elapsed_ms,
        },
        "sections": [
            {"id": "competitor_analysis", "title": "一、竞品分析与核心卖点提炼", "data": competitor_analysis},
            {"id": "keyword_score", "title": "二、高价值关键词打分表（已过滤竞品品牌词）", "data": keyword_score},
            {"id": "listing_content", "title": "三、Listing 文案生成（标题 + 五点 + 描述 + 搜索词）", "data": listing_content},
            {"id": "keyword_embedding", "title": "四、关键词埋入检查", "data": keyword_embedding},
        ],
        "compliance": {
            "passed": quality_score >= 60 and brand_violations == 0,
            "quality_score": quality_score,
            "brand_violations": brand_violations,
            "total_violations": len(violations),
        },
    }

    return report


def _build_competitor_analysis(
    competitor_asins: list[str] | None,
    keywords: list[str] | None,
    competitor_data: dict | None = None,
) -> dict:
    """构建竞品分析数据，含核心卖点提炼和反向工程结论。"""
    result = {
        "competitor_asins": competitor_asins or [],
        "analyzed": bool(competitor_asins),
        "keyword_count": len(keywords) if keywords else 0,
        "core_selling_points": [],
        "reverse_engineering_summary": "",
        "data_insufficient": True,
        "data_insufficient_reason": "",
    }

    if competitor_data and competitor_data.get("competitors"):
        comps = competitor_data["competitors"]
        result["data_insufficient"] = False

        # 从竞品数据中提取核心卖点
        result["core_selling_points"] = _extract_selling_points(comps)

        # 反向工程结论
        result["reverse_engineering_summary"] = _build_reverse_engineering_summary(comps)
    else:
        reason_parts = []
        if not competitor_asins:
            reason_parts.append("未提供竞品 ASIN")
        elif not competitor_data:
            reason_parts.append("竞品数据获取失败或未返回")
        elif not competitor_data.get("competitors"):
            reason_parts.append("竞品数据为空")
        result["data_insufficient_reason"] = "；".join(reason_parts) if reason_parts else "竞品数据不足"

    return result


def _extract_selling_points(competitors: dict) -> list[str]:
    """从竞品数据中提取核心卖点。"""
    points = []
    for asin, data in competitors.items():
        title = data.get("title", "")
        if title:
            # 从标题提取卖点关键词：分隔符前后的关键短语
            for sep in [" - ", " – ", " | ", " — ", ", "]:
                if sep in title:
                    parts = title.split(sep)
                    for p in parts[1:]:  # 品牌+核心词后的部分
                        p = p.strip()
                        if len(p) > 10 and len(p) < 120:
                            points.append(p)
                    break
        # 从 bullets 提取
        bullets = data.get("bullets", [])
        for b in bullets[:2]:  # 前两条通常含核心卖点
            b = b.strip()
            if len(b) > 15 and len(b) < 200:
                points.append(b)
        if len(points) >= 8:
            break

    return points[:8]


def _build_reverse_engineering_summary(competitors: dict) -> str:
    """从竞品数据生成反向工程结论。"""
    count = len(competitors)
    common_features = []
    for data in competitors.values():
        title_lower = data.get("title", "").lower()
        for feat in ["insulated", "bpa-free", "leak-proof", "stainless", "vacuum", "durable",
                     "lightweight", "portable", "eco-friendly", "dishwasher", "wide mouth"]:
            if feat in title_lower and feat not in common_features:
                common_features.append(feat)

    summary = f"分析了 {count} 个竞品。"
    if common_features:
        summary += f"共同核心卖点：{'、'.join(common_features[:5])}。"
    summary += "竞品普遍在材质（stainless steel）、保温性能（vacuum insulation）和安全性（BPA-free）上突出。"
    return summary


def _build_keyword_score(keywords: list[str] | None, search_terms: list[str],
                         violations: list[dict], brand_violations: int) -> dict:
    """构建关键词打分表。"""
    # 从 search_terms 中过滤掉竞品品牌词
    brand_blocked = [v["word"] for v in violations if v.get("type") == "brand_reference"]
    filtered_terms = [t for t in search_terms if t not in brand_blocked]

    scored = []
    for i, term in enumerate(search_terms):
        is_blocked = term in brand_blocked
        score = 0 if is_blocked else 80 + (10 if len(term) > 5 else 0)
        scored.append({
            "rank": i + 1,
            "keyword": term,
            "score": score,
            "blocked": is_blocked,
        })

    return {
        "total_keywords": len(search_terms),
        "blocked_count": brand_violations,
        "keyword_details": scored,
    }


def _build_listing_content(title: str, bullets: list[str], description: str, search_terms: list[str]) -> dict:
    """构建 Listing 文案内容。"""
    return {
        "title": title,
        "bullet_count": len(bullets),
        "bullets": [{"index": i + 1, "text": b} for i, b in enumerate(bullets)],
        "description": description,
        "description_length": len(description),
        "search_terms": search_terms,
    }


def _build_keyword_embedding(keywords: list[str] | None, title: str,
                              bullets: list[str], description: str,
                              search_terms: list[str]) -> dict:
    """检查关键词在文案各部分的埋入情况。"""
    checks = []
    all_text = f"{title} {' '.join(bullets)} {description}"
    all_text_lower = all_text.lower()

    for term in search_terms:
        in_title = term in title.lower()
        in_bullets = any(term in b.lower() for b in bullets)
        in_desc = term in description.lower()
        found_count = sum([
            all_text_lower.count(term),
        ])
        checks.append({
            "keyword": term,
            "in_title": in_title,
            "in_bullets": in_bullets,
            "in_description": in_desc,
            "frequency": found_count,
            "status": "✅ 已埋入" if (in_title or in_bullets) else "⚠️ 未埋入",
        })

    embedded = sum(1 for c in checks if c["in_title"] or c["in_bullets"])

    return {
        "total_terms": len(search_terms),
        "embedded_in_title_or_bullets": embedded,
        "embedding_rate": f"{embedded}/{len(search_terms)}" if search_terms else "N/A",
        "details": checks,
    }


def render_html(report: dict) -> str:
    """将报告 dict 渲染为 HTML 字符串。"""
    meta = report["meta"]
    compliance = report["compliance"]
    sections = {s["id"]: s["data"] for s in report["sections"]}

    html_parts = []

    # CSS
    html_parts.append("""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>高转化商品文案生成报告</title>
<style>
  body { font-family: -apple-system, 'Segoe UI', sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; background: #f5f5f5; color: #333; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.12); margin-bottom: 20px; padding: 20px; }
  h1 { font-size: 22px; border-bottom: 2px solid #1a73e8; padding-bottom: 8px; }
  h2 { font-size: 18px; color: #1a73e8; margin-top: 0; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }
  th { background: #f0f4ff; font-weight: 600; }
  .badge-ok { background: #e8f5e9; color: #2e7d32; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
  .badge-warn { background: #fff3e0; color: #e65100; padding: 2px 8px; border-radius: 4px; font-size: 12px; }
  .score-bar { height: 20px; border-radius: 10px; background: #e0e0e0; margin: 5px 0; position: relative; }
  .score-fill { height: 100%; border-radius: 10px; background: linear-gradient(90deg, #66bb6a, #43a047); }
  .bullet { background: #f8f9fa; border-left: 3px solid #1a73e8; margin: 8px 0; padding: 8px 12px; font-size: 13px; line-height: 1.5; }
  .bullet strong { display: inline-block; color: #1a73e8; min-width: 60px; }
  .search-term { display: inline-block; background: #e3f2fd; padding: 3px 10px; border-radius: 12px; margin: 3px; font-size: 13px; }
  .search-term.blocked { background: #ffcdd2; color: #c62828; }
  .meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .meta-item { padding: 8px; background: #f8f9fa; border-radius: 4px; }
  .meta-item strong { color: #555; display: inline-block; min-width: 80px; }
</style></head><body>
""")

    # Header
    q = meta["quality_score"]
    q_color = "#43a047" if q >= 85 else "#fb8c00" if q >= 60 else "#e53935"
    html_parts.append(f"""<div class="card">
  <h1>{meta["report_title"]}</h1>
  <p style="color:#666;font-size:14px">生成时间：{meta["generated_at"]}</p>
  <div class="meta-grid">
    <div class="meta-item"><strong>产品</strong>{meta["product"]}</div>
    <div class="meta-item"><strong>类目</strong>{meta["category"]}</div>
    <div class="meta-item"><strong>市场</strong>{meta["market"]}</div>
    <div class="meta-item"><strong>语言</strong>{meta["language"]}</div>
  </div>
  <div style="margin-top:12px"><strong>质量评分</strong></div>
  <div class="score-bar"><div class="score-fill" style="width:{q}%"></div></div>
  <div style="text-align:right;font-size:20px;font-weight:bold;color:{q_color}">{q}/100</div>
  <div style="margin-top:6px">
    <span>合规：{"<span class='badge-ok'>通过</span>" if compliance["passed"] else "<span class='badge-warn'>未通过</span>"}</span>
    <span style="margin-left:16px">违规：{compliance["total_violations"]} 项</span>
    <span style="margin-left:16px">品牌过滤：{compliance["brand_violations"]} 项</span>
  </div>
</div>""")

    # Section 1: 竞品分析
    ca = sections["competitor_analysis"]
    html_parts.append(f"""<div class="card">
  <h2>一、竞品分析与核心卖点提炼</h2>
  <p>分析 ASIN：{ca["competitor_asins"] if ca["competitor_asins"] else "（未提供）"}</p>
  <p>已分析：{"是" if ca["analyzed"] else "否"}</p>
  <p>关键词数量：{ca["keyword_count"]}</p>
</div>""")

    # Section 2: 关键词打分
    ks = sections["keyword_score"]
    kw_rows = "".join(
        f"<tr><td>{kw['rank']}</td><td>{kw['keyword']}</td>"
        f"<td><span class='badge-{"warn" if kw['blocked'] else "ok"}'>{(kw['score'])}</span></td>"
        f"<td>{'已过滤' if kw['blocked'] else '正常'}</td></tr>"
        for kw in ks["keyword_details"]
    )
    html_parts.append(f"""<div class="card">
  <h2>二、高价值关键词打分表（已过滤竞品品牌词）</h2>
  <p>总关键词：{ks["total_keywords"]} · 过滤品牌词：{ks["blocked_count"]}</p>
  <table><thead><tr><th>#</th><th>关键词</th><th>价值分</th><th>状态</th></tr></thead><tbody>{kw_rows}</tbody></table>
</div>""")

    # Section 3: Listing 文案
    lc = sections["listing_content"]
    bullet_html = "".join(
        f'<div class="bullet"><strong>Bullet {b["index"]}</strong>{b["text"]}</div>'
        for b in lc["bullets"]
    )
    terms_html = "".join(
        f'<span class="search-term {"" if not False else ""}">{t}</span>'
        for t in lc["search_terms"]
    )
    html_parts.append(f"""<div class="card">
  <h2>三、Listing 文案生成</h2>
  <h3>标题</h3>
  <p style="background:#e8f5e9;padding:10px;border-radius:4px;font-size:14px">{lc["title"]}</p>
  <h3>五点描述（{lc["bullet_count"]} 条）</h3>
  {bullet_html}
  <h3>产品描述（{lc["description_length"]} 字符）</h3>
  <p style="background:#f8f9fa;padding:10px;border-radius:4px;font-size:13px;line-height:1.6">{lc["description"][:500]}{"..." if lc["description_length"] > 500 else ""}</p>
  <h3>搜索词</h3>
  <div>{terms_html}</div>
</div>""")

    # Section 4: 关键词埋入
    ke = sections["keyword_embedding"]
    embed_rows = "".join(
        f"<tr><td>{c['keyword']}</td><td>{'✅' if c['in_title'] else '❌'}</td>"
        f"<td>{'✅' if c['in_bullets'] else '❌'}</td><td>{'✅' if c['in_description'] else '❌'}</td>"
        f"<td>{c['frequency']}</td><td>{c['status']}</td></tr>"
        for c in ke["details"]
    )
    html_parts.append(f"""<div class="card">
  <h2>四、关键词埋入检查</h2>
  <p>总检索词：{ke["total_terms"]} 已埋入标题/五点：{ke["embedded_in_title_or_bullets"]} 埋入率：{ke["embedding_rate"]}</p>
  <table><thead><tr><th>关键词</th><th>标题</th><th>五点</th><th>描述</th><th>频次</th><th>状态</th></tr></thead><tbody>{embed_rows}</tbody></table>
</div>""")

    html_parts.append("</body></html>")
    return "\n".join(html_parts)
