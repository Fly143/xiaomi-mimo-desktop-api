# xiaomi-mimo-desktop-api

将 **小米 MiMo Desktop 账号会话** 转换为 OpenAI / Anthropic 兼容 API。

基于 [MiMo2API](https://github.com/Fly143/MiMo2API)：保留 OpenAI/Anthropic 协议层与多账号，把上游从网页 bot（`aistudio.../bot/chat`）换成 **Desktop 账号会话**。

| | 旧 MiMo2API | 本仓库 |
|--|-------------|--------|
| 上游 | `aistudio.../open-apis/bot/chat` 网页 SSE | `mimo-server-cn.../api/route/chat/completions` |
| 鉴权 | 手贴 `serviceToken` cookie | `passToken` → SSO → `serviceToken`（自动续） |
| 官方 `api.xiaomimimo.com` | 不代理 | **不代理** |

## 链路

```
passToken (Desktop cookie 库)
  → passportapi / mimopc SSO
  → serviceToken cookie (30min 缓存, 401 重试)
  → POST /api/route/chat/completions   (OpenAI 兼容)
```

所有模型（含 `mimo-x-pro-preview` / `mimo-x-flash-preview`）都走这条 Desktop 会话通路。

## 快速开始

```bash
pip install -r requirements.txt
# 先登录一次 MiMo Desktop（会写 passToken 到 cookie 库）
# Desktop 运行时会独占锁 cookie DB，导入前建议先退出 Desktop
python main.py
# http://127.0.0.1:8080
```

管理页 `/` → Basic `admin` / `config.json` 的 `admin_password` → 自动检测 → 导入。

```bash
curl -u admin:change-me http://127.0.0.1:8080/api/desktop/auto-import
curl -u admin:change-me -X POST http://127.0.0.1:8080/api/desktop/import \
  -H 'Content-Type: application/json' -d @import.json
```

## 调用

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-pro","messages":[{"role":"user","content":"hi"}]}'
```

## 凭证

| 字段 | 含义 |
|------|------|
| `mimo_pass_token` | Desktop cookie 库 `passToken`（SSO 会话根） |
| `mimo_user_id` / `mimo_c_user_id` | 账号标识 cookie |

敏感字段 Fernet 加密落盘（`enc:v1:`），密钥在 `.secret_key`。

- `config.json` 与 `.secret_key` 均已 gitignore
- **备份时两个文件一起备**，丢了 `.secret_key` 密文无法恢复
- Docker 需同时挂载这两个文件

## 安全

- 默认 `HOST=127.0.0.1`
- 暴露端口前改掉 `admin_password` / `api_keys`
- `passToken` ≈ 账号登录态，备份时与 `.secret_key` 一起保管
