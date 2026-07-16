# Listing Generator — 鼎一跨境 AI Listing 生成服务

> PRD v2.5 | Worker: Listing | 最后更新: 2026-07-17

## 技术栈

| 组件 | 版本 |
|------|------|
| Python | 3.13 |
| Flask | 3.x |
| Gunicorn | 21.x |
| PyJWT | 2.10+ |
| SQLite | 3.x (本地任务状态持久化) |
| PCE (平台核心引擎) | HTTP API 集成 |

## 依赖安装

```bash
cd listing
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENGINE_JWT_SECRET` | JWT 签名密钥（与 PCE 共享，必填） | — |
| `PCE_API_BASE` | PCE 服务地址 | `http://127.0.0.1:8180` |
| `PCE_JWT_TOKEN` | PCE 静态 service token（备用） | — |
| `LISTING_TASK_DB` | 本地任务状态 SQLite 路径 | `listing/task_states.db` |
| `LISTING_DISPATCHER_WORKERS` | 异步 worker 线程数 | 2 |

## 开发

```bash
cd listing
source venv/bin/activate
ENGINE_JWT_SECRET=<secret> python3 -m flask --app app run --port 5001 --debug
```

## 测试

```bash
# py_compile 全部模块
python3 -m py_compile listing/*.py

# E2E 测试（需 PCE 运行）
ENGINE_JWT_SECRET=<secret> python3 -m flask --app app run --port 5001 &
# 发送请求:
curl -X POST http://127.0.0.1:5001/api/v1/listing/generate/react \
  -H "Authorization: Bearer <jwt>" \
  -H "X-Tenant-ID: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"product_name":"Test","category":"Sports","keywords":["a"],"selling_points":["b"],"target_market":"US","language":"en"}'
```

## 部署

```bash
cd listing
PCE_API_BASE=http://127.0.0.1:8180 \
ENGINE_JWT_SECRET=<secret> \
venv/bin/gunicorn -w 2 -b 127.0.0.1:5001 --timeout 90 --daemon app:app
```

## 架构

```
listing/
├── app.py                  # Flask 路由入口
├── pce_task_client.py      # PCE Task API 客户端（JWT 认证 + tenant 传播）
├── listing_generator.py    # ReAct Agent Listing 生成核心
├── react_agent.py          # ReAct 代理引擎
├── evidence_collector.py   # 并行证据采集（ISR 竞品/关键词）
├── quality_reviewer.py     # Pro 独立质量评审
├── compliance_checker.py   # 合规检查（亚马逊政策）
├── compliance_api.py       # 合规 API 集成
├── compliance_rules.py     # 合规规则定义
├── image_extractor.py      # 图片信息提取
├── publisher.py            # 多平台格式化发布
├── variant_generator.py    # 变体拆分
├── report_generator.py     # 报告生成
├── task_state.py           # 本地任务状态机（pending→running→completed/failed）
├── dispatcher.py           # 异步调度器（ThreadPoolExecutor）
├── status_endpoint.py      # Task status/checkpoint/receipt API
├── mcp_register.py         # MCP 工具注册
├── requirements.txt        # Python 依赖
└── README.md               # 本文件
```

## 安全

- **JWT 签名验证**: PyJWT HMAC-SHA256，拒绝 alg:none/坏签名/过期
- **Tenant 隔离**: JWT tenant_id + DB 租户级查询，跨租户 403
- **PCE 不可用**: 返回 `accepted=false`（不降级本地受理）
- **幂等**: `(tenant_id, idempotency_key)` 本地映射表
