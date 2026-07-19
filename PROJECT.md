# PROJECT.md — 鼎一 Listing 生成 Plugin

**创建时间**：2026-07-06
**阶段**：R122 文档收口完成（PRD v2.5，等待 R123 Codex C1 终验）
**负责人**：江信颖

---

## 🔐 服务器部署配置（强制查阅）

> ⚠️ 所有部署操作前必须先查阅总管手册《鼎一服务器配置与GitHub版本管理说明手册-v2.0-20260617-1754.md》

- **服务器**：`root@120.79.20.232`（阿里云 ECS，深圳，Ubuntu 24.04）
- **SSH 认证**：密码认证（sshpass）
- **SSH 密码**：`DingYi_aiagent_20260602`
- **标准 SSH 连接**：
  ```bash
> [REDACTED: inline development credential removed] Credentials are injected outside Git through a user-controlled development secret store.
  ```
- **本 Worker 部署路径**：`/opt/tools/tool-amazon/listing/`（与 ISR 同仓库 `tool-amazon`）
- **部署命令**：
  ```bash
> [REDACTED: inline development credential removed] Credentials are injected outside Git through a user-controlled development secret store.
    -e "ssh -o StrictHostKeyChecking=no" \
    . root@120.79.20.232:/opt/tools/tool-amazon/listing/
  ```
> [REDACTED: inline development credential removed] Credentials are injected outside Git through a user-controlled development secret store.
  > ⚠️ 当前设计与 ISR 同库同服务；若改为独立 Gunicorn 服务，需新建 `dingyi-listing.service` 并新增 Nginx `location /listing/`
- **Nginx 反代**：待添加 `location /listing/ { proxy_pass http://127.0.0.1:5000/listing/; }` 至 `/etc/nginx/sites-enabled/ai-dashboard`（或独立 server block）
- **健康检查**：`curl https://ai.hydrationflask.cn/listing/health`
> [REDACTED: inline development credential removed] Credentials are injected outside Git through a user-controlled development secret store.

## 技术栈

| 层级 | 技术 | 版本 |
|------|------|------|
| 运行时 | Python | 3.13.12 |
| Web 框架 | Flask | 最新 |
| WSGI 服务器 | Gunicorn | 最新 |
| LLM Client | PceLLMClient | PCE SDK /api/v1/llm/call |
| HTTP 客户端 | urllib (stdlib) | 3.13 |

---

## LLM Client — PceLLMClient

复用 ISR Worker 的 PceLLMClient（参考 `platform-core-engine/sdk/python/platform_core_sdk/llm.py` 适配）。

**调用方式**：
```
POST {PCE_API_BASE}/api/v1/llm/call
{
  "tag": "listing-generation",
  "model_hint": "pro",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ]
}
```

**标签映射**（PCE `cmd/engine/main.go` RegisterPreferredTag）：
```
listing-generation → deepseek-v4-pro（tag alias；DB config model=deepseek-chat）
```

**PCE 部署状态**（2026-07-17 更新）：
- 生产服务器（120.79.20.232）：PCE 已部署，运行在 `:8180`（systemd: dingyi-engine）
- LLM config 存于 `/opt/tools/platform-core-engine/data/llm.db`（status 须为 "online"）
- Listing 服务运行在 `127.0.0.1:5001`（gunicorn 独立进程，非 tool-amazon-prod）

---

## PCE MCP 注册

通过 PCE `/call` 端点调用 `listing-generation` 标签。

**能力注册**（L2 级别）：
```
MCP capability: listing-generate
描述：根据产品信息生成 Amazon listing 文案（标题、五点、描述、搜索词）
输入：product_name, category, keywords, selling_points, target_market, language
输出：title, bullets[], description, search_terms[], quality_score
标签：listing-generation
```

---

## 合规词库

复用 ISR 品类知识库 + Amazon 禁售/限售词表：
- 禁售词：firearm, weapon, drug, tobacco, alcohol, medication, prescription, hazardous, explosive
- 限售词：supplement, vitamin, pet food, battery, chemical
- 侵权风险词：Nike, Adidas, Apple, Disney, Marvel, Harry Potter, LEGO, Pokemon

---

## 部署目标

| 项目 | 值 |
|------|-----|
| 子域名 | `listing.hydrationflask.cn`（待申请） |
| 部署路径 | `/opt/tools/tool-amazon/listing/`（与 ISR 同仓库） |
| 运行方式 | Gunicorn（app:app） |
| Nginx 反代 | 待添加 `location /listing/` 或独立 server block |

**部署流程**（遵循手册 §十）：
1. git add + commit + push
2. rsync 代码到服务器
3. systemctl restart tool-amazon-prod
4. curl 健康检查验证

---

## 目录结构

> 完整架构见 `listing/README.md`（R122 交付）

```
listing/
├── README.md / API.md        # R122 文档（技术栈/API 参考）
├── app.py                    # Flask 主入口（JWT 验证 + 租户隔离）
├── listing_generator.py      # Prompt 工程 + PCE /api/v1/llm/call
├── react_agent.py            # ReAct 代理引擎
├── task_state.py             # 本地 SQLite 状态机 + 幂等映射
├── dispatcher.py             # ThreadPoolExecutor 真实调度
├── status_endpoint.py        # task status/checkpoint/receipt API
├── pce_task_client.py        # PCE Task API 客户端
├── evidence_collector.py     # ISR 并行证据采集（tenant 传播）
├── quality_reviewer.py       # Pro 独立评审
├── compliance_*.py           # 合规检查（3 文件）
├── image_extractor.py / publisher.py / variant_generator.py / report_generator.py
├── mcp_register.py           # MCP 注册脚本
├── requirements.txt          # flask/gunicorn/requests/PyJWT
└── .env.example
```
