"""图片→属性预填提取器 — 零输入冷启动。

通过 PCE LLM 从商品图片 URL 提取产品属性。
支持 URL 输入，当 PCE 支持视觉模型时自动升级。
"""

import json
import logging
import os
import re
import time
from typing import Any
from urllib.request import Request, urlopen, urlretrieve
from urllib.error import URLError, HTTPError

logger = logging.getLogger("listing-image")

PCE_API_BASE = os.environ.get("PCE_API_BASE", "http://127.0.0.1:8180")


def extract_from_image(image_url: str | None = None,
                       image_base64: str | None = None) -> dict[str, Any]:
    """从商品图片提取产品属性。

    分三步尝试：
    1. 直接调用 PCE LLM vision（content array 格式）— 未来兼容
    2. 下载图片 → 上传到 PCE → 文本 prompt 含 URL — 部分工作
    3. 纯文本推理（基于产品名称推断）

    Args:
        image_url: 1688 商品图片 URL
        image_base64: Base64 编码的图片数据（未使用，保留接口兼容）

    Returns:
        dict: { product_name, product_name_cn, category, attributes{}, extracted_text, confidence, vision_available }
    """
    if not image_url and not image_base64:
        raise ValueError("必须提供 image_url 或 image_base64")

    start = time.time()
    logger.info(f"Extracting from image: {image_url[:80] if image_url else 'base64'}...")

    # Step 1: 尝试 vision API（PCE 未来支持后自动生效）
    if image_url:
        result = _try_vision_api(image_url)
        if result:
            elapsed = time.time() - start
            result["elapsed_ms"] = int(elapsed * 1000)
            result["vision_available"] = True
            return result

    # Step 2: 下载图片 → 上传 PCE → 文本 prompt
    if image_url:
        result = _try_upload_and_llm(image_url)
        if result:
            elapsed = time.time() - start
            result["elapsed_ms"] = int(elapsed * 1000)
            result["vision_available"] = False
            return result

    # Step 3: 纯文本推理
    result = _text_only_inference(image_url or "product_image")
    elapsed = time.time() - start
    result["elapsed_ms"] = int(elapsed * 1000)
    result["vision_available"] = False
    return result


def _try_vision_api(image_url: str) -> dict | None:
    """尝试使用 PCE LLM vision API（content array 格式）。"""
    system_prompt = """You are an e-commerce product attribute extraction expert. Analyze the product image and extract:

1. Product name (Chinese + English)
2. Amazon category path (e.g., "Sports & Outdoors > Water Bottles")
3. Material
4. Dimensions/Capacity
5. Color/Style
6. Key Features (3-5 items)
7. Any visible text/specifications

Return ONLY valid JSON:
```json
{"product_name_cn":"","product_name_en":"","category":"","attributes":{"material":"","capacity":"","color":"","features":[],"dimensions":""},"extracted_text":"","confidence":0.0}
```"""

    try:
        body = {
            "tag": "listing-generation",
            "model_hint": "pro",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps([
                    {"type": "text", "text": "Analyze this 1688 product image and extract attributes."},
                    {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                ])},
            ],
        }
        req = Request(
            f"{PCE_API_BASE}/api/v1/llm/call",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data.get("data", {}).get("content", "") or data.get("content", "")
        if content:
            result = _parse_json_result(content)
            if result and result.get("confidence", 0) > 0.3:
                return result
    except (HTTPError, URLError, json.JSONDecodeError, OSError) as e:
        logger.info(f"Vision API not available ({type(e).__name__}), trying upload+LLM")
    return None


def _try_upload_and_llm(image_url: str) -> dict | None:
    """下载图片 → 上传到 PCE → 文本 prompt 含 URL → LLM 推理。"""
    try:
        # 下载图片
        local_path = f"/tmp/img_extract_{int(time.time())}.jpg"
        urlretrieve(image_url, local_path)

        # 上传到 PCE
        import subprocess
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{PCE_API_BASE}/api/v1/upload/image",
             "-F", f"image=@{local_path}"],
            capture_output=True, text=True, timeout=15,
        )
        upload_data = json.loads(result.stdout)
        pce_url = upload_data.get("url", "")
        os.unlink(local_path)

        if not pce_url:
            return None

        logger.info(f"Image uploaded to PCE: {pce_url}")

        # 使用文本 prompt 传递图片 URL
        system_prompt = """You are an e-commerce product attribute extraction expert. 
Based on the product image URL provided below, infer the product attributes for an Amazon listing.
The image URL points to a product image from 1688 (Chinese wholesale marketplace).

Extract:
1. Product name (Chinese + English)
2. Amazon category path
3. Material
4. Dimensions/Capacity
5. Color
6. Key Features (3-5)
7. Any visible text visible in the image

If you cannot see the image, make your best guess based on the URL pattern.

Return ONLY valid JSON:
```json
{"product_name_cn":"","product_name_en":"","category":"","attributes":{"material":"","capacity":"","color":"","features":[],"dimensions":""},"extracted_text":"","confidence":0.0}
```"""

        body = {
            "tag": "listing-generation",
            "model_hint": "pro",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Product image URL: {pce_url}\n\nExtract attributes from this image."},
            ],
        }
        req = Request(
            f"{PCE_API_BASE}/api/v1/llm/call",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data.get("data", {}).get("content", "") or data.get("content", "")
        if content:
            result = _parse_json_result(content)
            if result:
                return result
    except Exception as e:
        logger.info(f"Upload+LLM failed ({type(e).__name__}), using text-only inference")
    return None


_TEMPLATES = {
    "bottle": {"name": "Water Bottle", "category": "Sports & Outdoors > Water Bottles",
               "features": ["Portable", "Durable", "Leak-proof"]},
    "cup": {"name": "Travel Mug", "category": "Kitchen & Dining > Travel Mugs",
            "features": ["Insulated", "Spill-proof", "Portable"]},
    "phone": {"name": "Phone Case", "category": "Electronics > Phone Cases",
              "features": ["Shockproof", "Slim", "Durable"]},
    "bag": {"name": "Backpack", "category": "Luggage > Backpacks",
            "features": ["Spacious", "Lightweight", "Comfortable"]},
    "shoe": {"name": "Running Shoes", "category": "Clothing > Shoes",
             "features": ["Breathable", "Comfortable", "Non-slip"]},
    "toy": {"name": "Educational Toy", "category": "Toys > Educational Toys",
            "features": ["Safe", "Educational", "Interactive"]},
}


def _text_only_inference(url_or_text: str) -> dict:
    """纯文本推理（based on URL pattern matching）。"""
    url_lower = url_or_text.lower()
    confidence = 0.35

    # 尝试匹配品类关键词
    matched_template = None
    for keyword, template in _TEMPLATES.items():
        if keyword in url_lower:
            matched_template = template
            confidence = 0.45
            break

    if matched_template:
        return {
            "product_name": matched_template["name"],
            "product_name_cn": "",
            "category": matched_template["category"],
            "attributes": {
                "material": "",
                "capacity": "",
                "color": "",
                "features": matched_template["features"],
                "dimensions": "",
            },
            "extracted_text": "",
            "confidence": confidence,
        }

    return {
        "product_name": "Product",
        "product_name_cn": "",
        "category": "",
        "attributes": {
            "material": "",
            "capacity": "",
            "color": "",
            "features": [],
            "dimensions": "",
        },
        "extracted_text": "",
        "confidence": 0.2,
    }


def _parse_json_result(content: str) -> dict | None:
    """从 LLM 响应中提取 JSON。"""
    content = content.strip()

    for marker in ["```json", "```"]:
        if marker in content:
            start = content.index(marker) + len(marker)
            end = content.index("```", start) if "```" in content[start:] else len(content)
            content = content[start:end].strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 尝试 JSON 修复
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            data = json.loads(content[start:end])
        except (ValueError, json.JSONDecodeError):
            return None

    attributes = data.get("attributes", {}) or {}
    result = {
        "product_name": data.get("product_name_en") or data.get("product_name", ""),
        "product_name_cn": data.get("product_name_cn", ""),
        "category": data.get("category", ""),
        "attributes": {
            "material": attributes.get("material", ""),
            "capacity": attributes.get("capacity", ""),
            "color": attributes.get("color", ""),
            "features": attributes.get("features", []),
            "dimensions": attributes.get("dimensions", ""),
        },
        "extracted_text": data.get("extracted_text", ""),
        "confidence": data.get("confidence", 0.3),
    }

    if not result["product_name"]:
        result["product_name"] = result["product_name_cn"]
    return result
