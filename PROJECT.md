# PROJECT.md — 鼎一 Listing 生成 Plugin

**创建时间**：2026-07-06
**阶段**：Phase 1 — MVP 开发
**负责人**：江信颖

---

## 🔐 服务器部署配置（强制查阅）

> ⚠️ 所有部署操作前必须先查阅总管手册《鼎一服务器配置与GitHub版本管理说明手册-v2.0-20260617-1754.md》

- **服务器**：`root@120.79.20.232`（阿里云 ECS，深圳，Ubuntu 24.04）
- **SSH 认证**：密码认证（sshpass）
- **SSH 密码**：`DingYi_aiagent_20260602`
- **标准 SSH 连接**：
  ```bash
  sshpass -p 'DingYi_aiagent_20260602' ssh -o StrictHostKeyChecking=no root@120.79.20.232
  ```
- **本 Worker 部署路径**：`/opt/tools/tool-amazon/listing/`（与 ISR 同仓库 `tool-amazon`）
- **部署命令**：
  ```bash
  sshpass -p 'DingYi_aiagent_20260602' rsync -avz --delete \
    -e "ssh -o StrictHostKeyChecking=no" \
    . root@120.79.20.232:/opt/tools/tool-amazon/listing/
  ```
- **服务重启**：`sshpass -p 'DingYi_aiagent_20260602' ssh -o StrictHostKeyChecking=no root@120.79.20.232 "systemctl restart tool-amazon-prod"`
  > ⚠️ 当前设计与 ISR 同库同服务；若改为独立 Gunicorn 服务，需新建 `dingyi-listing.service` 并新增 Nginx `location /listing/`
- **Nginx 反代**：待添加 `location /listing/ { proxy_pass http://127.0.0.1:5000/listing/; }` 至 `/etc/nginx/sites-enabled/ai-dashboard`（或独立 server block）
- **健康检查**：`curl https://ai.hydrationflask.cn/listing/health`
- **回滚**：`sshpass -p 'DingYi_aiagent_20260602' ssh -o StrictHostKeyChecking=no root@120.79.20.232 "cp -r /opt/tools/tool-amazon/listing /opt/tools/tool-amazon/listing.bak.$(date +%Y%m%d_%H%M)"`

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

**标签映射**（PCE `cmd/engine/main.go` 第 88 行）：
```
listing-generation → deepseek-v4-pro
```

**实测延迟**：1355ms（PCE STATUS.md 确认）

**PCE 部署状态**：
- 生产服务器（120.79.20.232）：PCE 尚未部署（PRE-06 待执行），当前 ISR 直连 DeepSeek API
- 本地开发环境：PCE 运行在 `localhost:8080`，listing-generation 标签已就绪

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

```
listing/
├── PROJECT.md
├── app.py                    # Flask 主入口
├── listing_generator.py      # Prompt 工程 + PCE /call 调用
├── compliance_checker.py     # 合规词库校验
├── mcp_register.py           # MCP 注册脚本
├── requirements.txt
├── .env.example
└── status/
    └── STATUS.md
```
