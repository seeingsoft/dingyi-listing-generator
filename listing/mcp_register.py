"""MCP 注册脚本 — 向 PCE 注册 listing-generate 能力。

向 PCE MCP Server 注册 listing-generate 能力（L2 级别）。
PCE 通过 /call 端点暴露此能力，tag=listing-generation。
"""

import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

PCE_API_BASE = os.environ.get("PCE_API_BASE", "http://localhost:8080")

LISTING_CAPABILITY = {
    "name": "listing-generate",
    "version": "1.0.0",
    "level": "L2",
    "description": "根据产品信息生成 Amazon listing 文案（标题、五点描述、产品描述、搜索词）",
    "tags": ["listing-generation"],
    "input_schema": {
        "type": "object",
        "properties": {
            "product_name": {"type": "string", "description": "产品名称"},
            "category": {"type": "string", "description": "Amazon 类目路径"},
            "keywords": {"type": "array", "items": {"type": "string"}, "description": "搜索关键词"},
            "selling_points": {"type": "array", "items": {"type": "string"}, "description": "核心卖点"},
            "target_market": {"type": "string", "enum": ["US", "UK", "DE", "JP", "CA", "FR", "IT", "ES"], "default": "US"},
            "language": {"type": "string", "enum": ["en", "de", "ja", "fr", "it", "es", "zh"], "default": "en"},
        },
        "required": ["product_name"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Amazon 标题（≤200 字符）"},
            "bullets": {"type": "array", "items": {"type": "string"}, "description": "五点描述"},
            "description": {"type": "string", "description": "产品描述"},
            "search_terms": {"type": "array", "items": {"type": "string"}, "description": "后台搜索词"},
            "quality_score": {"type": "integer", "description": "质量分（0-100）"},
        },
    },
    "callback": {
        "endpoint": os.environ.get("LISTING_ENDPOINT", "http://localhost:5001/api/v1/listing/generate"),
        "method": "POST",
    },
}


def register_capability():
    """向 PCE MCP Server 注册 listing-generate 能力。"""
    url = f"{PCE_API_BASE}/api/v1/mcp/capabilities"
    body = json.dumps(LISTING_CAPABILITY).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"})

    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except HTTPError as e:
        print(f"MCP register failed: HTTP {e.code}")
        print(e.read().decode("utf-8", errors="ignore")[:500])
        sys.exit(1)
    except URLError as e:
        print(f"MCP register failed: {e.reason}")
        sys.exit(1)


def verify_capability():
    """验证 listing-generate 能力是否已注册。"""
    url = f"{PCE_API_BASE}/api/v1/mcp/capabilities/listing-generate"
    req = Request(url, headers={"Content-Type": "application/json"})

    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except HTTPError as e:
        if e.code == 404:
            print("listing-generate capability not found (not yet registered)")
            return None
        print(f"Verify failed: HTTP {e.code}")
        return None
    except URLError as e:
        print(f"Verify failed: {e.reason}")
        return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MCP capability registration for listing-generate")
    parser.add_argument("action", choices=["register", "verify", "both"], default="both", nargs="?",
                        help="register: 注册能力 | verify: 验证能力 | both: 注册后验证")
    args = parser.parse_args()

    if args.action in ("register", "both"):
        print(f"Registering 'listing-generate' capability to {PCE_API_BASE}...")
        result = register_capability()
        print(f"Result: {json.dumps(result, indent=2, ensure_ascii=False)}")

    if args.action in ("verify", "both"):
        result = verify_capability()
        if result:
            print(f"Verified: {json.dumps(result, indent=2, ensure_ascii=False)}")
        else:
            print("Capability not verified (PCE MCP API may not support GET)")
