# Listing API 文档

> PRD v2.5 | 版本: 1.0 | 更新: 2026-07-17

## 通用说明

- **Base URL**: `http://127.0.0.1:5001`
- **认证**: 所有端点需 `Authorization: Bearer <JWT>`。JWT 须含 `tenant_id`、`exp` claims
- **租户隔离**: 查询类端点需 `X-Tenant-ID` header，与 JWT tenant_id 不一致返回 403
- **PCE 不可用**: 创建类端点返回 `accepted=false, error="pce_unavailable"`

---

## 1. Listing 生成

### POST /api/v1/listing/generate/react

生成 Amazon Listing 文案（ReAct Agent 模式）。

**Headers**:
```
Authorization: Bearer <JWT>
X-Tenant-ID: <UUID>
Content-Type: application/json
```

**Request Body (sync)**:
```json
{
  "product_name": "Insulated Water Bottle",
  "category": "Sports > Bottles",
  "keywords": ["insulated", "stainless"],
  "selling_points": ["24h cold", "leakproof"],
  "target_market": "US",
  "language": "en"
}
```

**Request Body (async)**:
```json
{
  "product_name": "Insulated Water Bottle",
  "category": "Sports > Bottles",
  "keywords": ["insulated"],
  "selling_points": ["leakproof"],
  "target_market": "US",
  "language": "en",
  "async": true
}
```

**Response (sync, 200)**:
```json
{
  "success": true,
  "data": {
    "title": "...",
    "bullets": ["...", "..."],
    "description": "...",
    "search_terms": ["..."]
  },
  "compliance": {
    "passed": true,
    "violations": [],
    "quality_score": 85,
    "brand_violations": 0
  },
  "mode": "react",
  "task_id": "lst-xxxxxxxx-xxxxxxxxx",
  "evidence_graph": {
    "total_claims": 5,
    "claims": [...],
    "tenant_id": "..."
  },
  "pro_review": {...}
}
```

**Response (async, 200)**:
```json
{
  "task_id": "lst-xxxxxxxx-xxxxxxxxx",
  "pce_task_id": "task-listing_generation-...",
  "accepted": true,
  "mode": "react",
  "status_url": "/api/v1/tasks/lst-...",
  "receipt_url": "/api/v1/tasks/lst-.../receipt"
}
```

**Response (PCE unavailable, 200)**:
```json
{
  "accepted": false,
  "error": "pce_unavailable",
  "detail": "PCE CreateTask failed or timed out - request rejected (no local fallback)"
}
```

**Response (idempotent, 200)**:
```json
{
  "task_id": "lst-xxxxxxxx-xxxxxxxxx",
  "accepted": true,
  "idempotent": true,
  "mode": "react"
}
```

**Error (401 — 无 JWT 或无效 JWT)**:
```json
{"error": "JWT validation failed"}
```

**Error (403 — JWT tenant 与 Header 冲突)**:
```json
{"error": "X-Tenant-ID header mismatch JWT tenant: header=X jwt=Y"}
```

---

## 2. Task 状态查询

### GET /api/v1/tasks/{task_id}

查询任务状态（需 X-Tenant-ID）。

**Response (200)**:
```json
{
  "task_id": "lst-xxx",
  "tenant_id": "00000000-0000-0000-0000-000000000001",
  "status": "completed",
  "payload": {...},
  "result": {...},
  "created_at": 1784219625.123,
  "updated_at": 1784219626.456,
  "completed_at": 1784219626.456
}
```

**Error (401)**: `X-Tenant-ID header required`

**Error (403)**: `forbidden: task belongs to different tenant`

**Error (404)**: `task not found`

### GET /api/v1/tasks

列出当前租户的所有任务。

**Query Parameters**:
- `status` (可选): 按状态过滤 (pending/running/completed/failed)
- `limit` (可选): 返回数量上限，默认 20

---

## 3. Task Checkpoint

### POST /api/v1/tasks/{task_id}/checkpoint

写入任务阶段 checkpoint。

**Request Body**:
```json
{
  "phase": "react_done",
  "detail": {"title": "..."}
}
```

### GET /api/v1/tasks/{task_id}/checkpoints

查询任务的所有 checkpoint。

**Response**:
```json
{
  "task_id": "lst-xxx",
  "checkpoints": [
    {"phase": "task_created", "detail": {}, "created_at": 123.456},
    {"phase": "dispatch_started", "detail": {}, "created_at": 123.457},
    {"phase": "react_start", "detail": {}, "created_at": 123.458}
  ],
  "total": 3
}
```

---

## 4. Task Receipt

### GET /api/v1/tasks/{task_id}/receipt

生成任务完成回执（含全量 checkpoint 链和耗时）。

**Response**:
```json
{
  "receipt_id": "rcpt-lst-xxx",
  "task_id": "lst-xxx",
  "tenant_id": "00000000-0000-0000-0000-000000000001",
  "status": "completed",
  "created_at": 123.456,
  "completed_at": 125.789,
  "duration_seconds": 2.333,
  "result": {...},
  "checkpoints": [...],
  "checkpoint_count": 4,
  "generated_at": 125.790
}
```

---

## 5. 健康检查

### GET /health

```json
{"status": "healthy", "service": "listing-generator"}
```

### GET /api/v1/listing/health

深度健康检查（含 PCE 连通性）:
```json
{
  "status": "healthy",
  "components": {
    "pce": {"status": "connected", "endpoint": "..."},
    "dispatcher": {"active_tasks": 0}
  }
}
```

---

## 租户隔离模型

```
┌─────────┐        JWT(tenant_id=A)        ┌──────────┐
│ Tenant A │ ────────────────────────────── │  Listing │
└─────────┘                                 │  API     │
                                            │          │
┌─────────┐  GET /tasks/tid  X-Tenant-ID=B  │  403 ←── │
│ Tenant B │ ─────────────────────────────→ │          │
└─────────┘                                 └──────────┘
```

- **创建**: JWT 签名验证后提取 tenant_id → 创建 task 归属该租户
- **查询**: `get_task_tenant(task_id)` 比较 Header 租户，不一致 403
- **幂等**: `(tenant_id, idempotency_key)` 本地映射，跨租户不冲突
