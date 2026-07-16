"""PRD v2.2 Phase 1 — 并行证据采集引擎。

R-004 阶段2：竞品分析/关键词分析/评论VOC/类目趋势/合规检查
五路并行采集 → 汇入 Evidence Graph。
"""

import concurrent.futures
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger("evidence-collector")

ISR_BASE = os.environ.get("ISR_API_BASE", "http://127.0.0.1:5000/api/v1")
SUBTASK_TIMEOUT = int(os.environ.get("EVIDENCE_SUBTASK_TIMEOUT", "30"))


class EvidenceClaim:
    """单条证据声明。"""

    def __init__(
        self,
        claim: str,
        source_type: str,
        source_id: str = "",
        market: str = "US",
        confidence: float = 0.5,
        provider: str = "",
        cached: bool = False,
        error: str | None = None,
    ):
        self.claim = claim
        self.source_type = source_type
        self.source_id = source_id
        self.market = market
        self.confidence = confidence
        self.provider = provider
        self.cached = cached
        self.error = error

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "market": self.market,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "confidence": self.confidence,
            "provider": self.provider,
            "cached": self.cached,
        }


def _fetch_parallel_evidence(
    asins: list[str] | None = None,
    keywords: list[str] | None = None,
    market: str = "US",
    tenant_id: str | None = None,
) -> list[dict]:
    """五路并行证据采集。

    R119: tenant_id 参数，传播到 ISR API 调用。

    当前实现：
    - ISR competitor-detail-batch（竞品数据）
    - ISR keyword-value（关键词数据）

    预留接入（Phase 2）：
    - 评论 VOC
    - 类目趋势
    - 合规检查

    Args:
        asins: 竞品 ASIN 列表
        keywords: 产品关键词列表
        market: 目标市场
        tenant_id: 租户 ID（R119，传播到 ISR）

    Returns:
        List[dict]: EvidenceClaim.to_dict() 列表，含 tenant_id
    """
    claims: list[EvidenceClaim] = []
    subtasks: list[dict] = []

    # 构建子任务
    if asins:
        subtasks.append({
            "name": "competitor-detail-batch",
            "func": _fetch_competitor_detail,
            "args": (asins, market, tenant_id),
        })
        subtasks.append({
            "name": "keyword-value",
            "func": _fetch_keyword_value,
            "args": (asins, market, tenant_id),
        })

    if not subtasks:
        logger.info("No subtasks to run (no ASINs)")
        return [ec.to_dict() for ec in claims]

    # 并行执行
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(subtasks)) as executor:
        future_map = {
            executor.submit(t["func"], *t["args"]): t["name"]
            for t in subtasks
        }

        for future in concurrent.futures.as_completed(future_map, timeout=SUBTASK_TIMEOUT * 2):
            name = future_map[future]
            try:
                result = future.result(timeout=SUBTASK_TIMEOUT)
                if result:
                    claims.extend(result)
                    logger.info(f"Subtask '{name}' returned {len(result)} claims")
            except concurrent.futures.TimeoutError:
                claims.append(EvidenceClaim(
                    claim=f"采集超时（>{SUBTASK_TIMEOUT}s）",
                    source_type=name,
                    error="timeout",
                    confidence=0.0,
                ))
                logger.warning(f"Subtask '{name}' timed out after {SUBTASK_TIMEOUT}s")
            except Exception as e:
                claims.append(EvidenceClaim(
                    claim=f"采集失败",
                    source_type=name,
                    error=str(e),
                    confidence=0.0,
                ))
                logger.warning(f"Subtask '{name}' failed: {e}")

    return [ec.to_dict() for ec in claims]


def _fetch_competitor_detail(asins: list[str], market: str, tenant_id: str | None = None) -> list[EvidenceClaim]:
    """调用 ISR competitor-detail-batch 获取竞品证据。

    R119: tenant_id 传播到 ISR。
    """
    headers = {}
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    resp = requests.post(
        f"{ISR_BASE}/search/competitor-detail-batch",
        json={"asins": asins},
        headers=headers or None,
        timeout=SUBTASK_TIMEOUT,
    )
    if not resp.ok:
        logger.warning(f"competitor-detail-batch returned {resp.status_code}")
        return []

    data = resp.json()
    competitors = data.get("competitors", {})
    claims = []

    for asin, comp in competitors.items():
        title = comp.get("title", "")
        if title:
            # 从竞品标题提取卖点 claim
            for sep in [" - ", " – ", " | ", " — "]:
                if sep in title:
                    parts = title.split(sep)
                    for p in parts[1:]:
                        p = p.strip()
                        if 10 < len(p) < 120:
                            claims.append(EvidenceClaim(
                                claim=p,
                                source_type="competitor_listing",
                                source_id=asin,
                                market=market,
                                confidence=0.75,
                                provider="isr",
                            ))
                    break

        # 从 bullets 提取 claim
        bullets = comp.get("bullets", [])
        for b in bullets[:3]:
            b = b.strip()
            if len(b) > 15:
                claims.append(EvidenceClaim(
                    claim=b[:180],
                    source_type="competitor_listing",
                    source_id=asin,
                    market=market,
                    confidence=0.65,
                    provider="isr",
                ))

        if len(claims) >= 12:
            break

    return claims


def _fetch_keyword_value(asins: list[str], market: str, tenant_id: str | None = None) -> list[EvidenceClaim]:
    """调用 ISR keyword-value 获取关键词证据。

    R119: tenant_id 传播到 ISR。
    """
    headers = {}
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    resp = requests.post(
        f"{ISR_BASE}/search/keyword-value",
        json={"asins": asins},
        headers=headers or None,
        timeout=SUBTASK_TIMEOUT,
    )
    if not resp.ok:
        logger.warning(f"keyword-value returned {resp.status_code}")
        return []

    data = resp.json()
    keywords = data.get("keywords", [])
    claims = []

    for kw in keywords[:10]:
        if isinstance(kw, dict):
            term = kw.get("keyword", kw.get("term", ""))
            volume = kw.get("search_volume", kw.get("volume", 0))
        else:
            term = str(kw)
            volume = 0

        if term:
            claims.append(EvidenceClaim(
                claim=term,
                source_type="keyword_data",
                source_id=term,
                market=market,
                confidence=min(0.5 + volume / 100000 * 0.4, 0.9),
                provider="isr",
            ))

    return claims


def build_evidence_graph(
    claims: list[dict],
    tenant_id: str | None = None,
) -> dict:
    """从 claims 列表构建 evidence_graph 结构。

    R119: tenant_id 参数写入输出。

    Args:
        claims: _fetch_parallel_evidence() 返回值
        tenant_id: 租户 ID（写入输出）

    Returns:
        dict: {
            "total_claims": int,
            "sources": list[str],
            "claims": list[dict],
            "data_insufficient": bool,
            "tenant_id": str | None,
        }
    """
    if not claims:
        return {
            "total_claims": 0,
            "sources": [],
            "claims": [],
            "data_insufficient": True,
            "tenant_id": tenant_id,
        }

    sources = list(set(c.get("source_type", "unknown") for c in claims))
    # 过滤 error 条目
    valid_claims = [c for c in claims if not c.get("error")]

    return {
        "total_claims": len(valid_claims),
        "sources": sorted(sources),
        "claims": valid_claims,
        "data_insufficient": len(valid_claims) < 3,
        "tenant_id": tenant_id,
    }
