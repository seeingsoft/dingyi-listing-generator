# Listing Worker — 项目状态总览

> 最后更新：2026-07-17 | 阶段：R122 文档收口完成，等待 R123 Codex C1 终验

## 当前状态

| 里程碑 | 状态 | 说明 |
|--------|:----:|------|
| R112-RECOVERY Gate D2（真实调度+请求级tenant） | ✅ | commit `2c6912e` |
| R112-RECOVERY Gate H（P8/L5/L2 修复） | ✅ | accepted=false + 403 + 幂等映射表 |
| R112-RECOVERY Gate L（JWT 签名验证） | ✅ | PyJWT HMAC-SHA256, commit `e4b56d4` |
| R112-RECOVERY Gate P（PyJWT 依赖声明） | ✅ | commit `d5e3bab` |
| R119（tenant 传播 + evidence 租户化） | ✅ | commit `f360270` |
| R120（全仓 E2E） | ✅ 5/5 | 双租户403/accepted/幂等/401/sync终态(quality=85) |
| R121（全量终版校验） | ✅ | py_compile 17/17 + E2E 4/4 |
| R122（文档收口） | ✅ | listing/README.md + listing/API.md, commit `a70869f` |
| R123（Codex C1 终验 + 方总 sign-off） | ⏸️ | 等待 coordinator |

## 核心能力

- **Listing 生成**：ReAct Agent + PCE LLM（listing-generation tag）+ 合规检查 + 证据图 + Pro 评审
- **任务状态机**：本地 SQLite（pending→running→completed/failed）+ checkpoint + receipt
- **真实调度**：ThreadPoolExecutor（max 2 workers），async 模式真实入队执行
- **安全**：PyJWT 签名验证（拒绝 alg:none/坏签名/过期）、租户隔离（跨租户 403）、幂等映射
- **失败语义**：PCE 不可用 → `accepted=false`，不降级本地受理

## 文档索引

| 文档 | 内容 |
|------|------|
| `listing/README.md` | 技术栈 / 环境变量 / 开发 / 测试 / 部署 / 架构 |
| `listing/API.md` | 5 端点 API 文档 + 租户隔离模型 |
| `PROJECT.md` | 服务器配置 / 部署流程 / 合规词库 |
| `status/STATUS.md` | Worker 协作指令与回执（coordinator 管理） |
| `status/CURRENT.json` | 机器可读的当前轮次状态 |
