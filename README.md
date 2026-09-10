# xiaomi-mimo-desktop-api

将 **小米 MiMo Desktop 账号会话** 转换为 **OpenAI + Anthropic 兼容 API**（Chat / Responses / Anthropic Messages / 工具调用 / 多账号）。

基于 [MiMo2API](https://github.com/Fly143/MiMo2API)：协议层、工具筛分、上下文压缩、Responses、Anthropic 兼容全部保留；**上游从网页 bot（`aistudio.../bot/chat`）换成 Desktop 账号会话**。

| | MiMo2API | 本仓库 |
|--|----------|--------|
| 上游 | `aistudio.../open-apis/bot/chat` 网页 SSE | `mimo-server-cn.../api/route/chat/completions` |
| 鉴权 | 手贴 `serviceToken` + `xiaomichatbot_ph` | `passToken` → SSO → `serviceToken`（自动续，30min 缓存） |
| 模型 | aistudio 全量 + TTS/ASR | **仅 Desktop 独占** `mimo-x-pro-preview` / `mimo-x-flash-preview` |
| 官方 `api.xiaomimimo.com` | 不代理 | 不代理 |
| TTS / ASR | 有 | **无**（Desktop 通路没有这些接口） |

## 链路

```
passToken (Desktop cookie 库)
  → passportapi / mimopc SSO
  → serviceToken cookie (30min 缓存, 401 重试)
  → POST /api/route/chat/completions   (OpenAI 兼容)
```

## 特性

- **OpenAI 兼容** — `/v1/chat/completions`（流式/非流式）、`/v1/models`
- **Anthropic Messages** — `/v1/messages` + count_tokens + batches
- **Responses API** — `/v1/responses*`
- **工具调用** — MiMoML / <tool_call> / TOOL_CALL 等多策略提取 + 流式筛分
- **上下文压缩** — 超长对话自动 compress / truncate
- **多账号** — passToken 轮询
- **凭证加密** — Fernet（`enc:v1:`），密钥 `.secret_key`
- **管理页** — Desktop 一键检测导入

## 快速开始

```bash
pip install -r requirements.txt
# 先登录一次 MiMo Desktop；Desktop 运行时会锁 cookie DB，导入前建议退出 Desktop
./deploy.sh   # 或 python main.py
# 默认 http://127.0.0.1:8080
```

管理页 `/` → Basic `admin` / `config.json` 的 `admin_password` → **自动检测 → 导入**。

```bash
curl -u admin:change-me http://127.0.0.1:8080/api/desktop/auto-import
curl -u admin:change-me -X POST http://127.0.0.1:8080/api/desktop/import \
  -H 'Content-Type: application/json' \
  -d '{"mimoPassToken":"...","mimoUserId":"...","uid":"..."}'
```

## 调用

```bash
# OpenAI
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-x-pro-preview","messages":[{"role":"user","content":"hi"}]}'

# Anthropic
curl http://127.0.0.1:8080/v1/messages \
  -H "x-api-key: sk-mimo" -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-6","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}'
```

Claude 模型名自动映射：opus 级 → `mimo-x-pro-preview`，sonnet/haiku → `mimo-x-flash-preview`。

## 项目结构

```
xiaomi-mimo-desktop-api/
├── main.py
├── deploy.sh
├── requirements.txt
├── config.example.json
├── config.json              # .gitignore，敏感字段已加密
├── .secret_key              # .gitignore，Fernet 密钥
├── Dockerfile
├── web/index.html           # 管理页（Desktop 导入）
└── app/
    ├── desktop_session.py   # passToken → SSO → serviceToken
    ├── auto_import.py       # 读 Desktop cookie 库
    ├── mimo_client.py       # Desktop /api/route 客户端
    ├── routes.py            # OpenAI / Responses / 管理
    ├── anthropic_routes.py  # Anthropic Messages
    ├── config.py            # 多账号 + Fernet 加密
    ├── tool_call.py / tool_sieve.py
    ├── context_compressor.py / session_store.py
    └── ...
```

## 凭证

| 字段 | 含义 |
|------|------|
| `mimo_pass_token` | Desktop cookie 库 `passToken` |
| `mimo_user_id` / `mimo_c_user_id` | 账号标识 cookie |

敏感字段 Fernet 加密落盘；`config.json` + `.secret_key` **一起备份**。

## 安全

- 默认 `HOST=127.0.0.1`
- 暴露端口前改掉 `admin_password` / `api_keys`
- `passToken` ≈ 账号登录态

## 限制

| 项 | 说明 |
|----|------|
| 模型 | 仅 `mimo-x-pro-preview` / `mimo-x-flash-preview` |
| TTS / ASR | 不提供（Desktop 无此接口） |
| 会话删除 | `/api/route` 无 conversation 删除，清理只动本地记录 |
| cookie 锁 | Desktop 运行时可能锁 cookie DB，导入失败先退出 Desktop |

## 许可

MIT
