# xiaomi-mimo-desktop-api

将 **小米 MiMo Desktop 会话** 与官方 API 转换为 OpenAI / Anthropic 兼容 API。

基于 [MiMo2API](https://github.com/Fly143/MiMo2API)，上游从网页 bot（`aistudio.../bot/chat`）替换为：

| 通路 | 端点 | 鉴权 |
|------|------|------|
| Desktop Preview | `mimo-server-cn.xiaomimimo.com/api/route/chat/completions` | `serviceToken` cookie（passToken SSO） |
| 官方 API | `api.xiaomimimo.com/v1/chat/completions` | `Authorization: Bearer sk-...` |

## 与原版差异

- 不再解析网页 bot SSE / MiMoML；上游已是 OpenAI 格式，直接代理
- `passToken` 自动从本机 MiMo Desktop cookie 库读取，SSO 换 `serviceToken`，30min 缓存 + 401 重试
- 支持 `mimo-x-pro-preview` / `mimo-x-flash-preview`
- 保留 Anthropic `/v1/messages`、工具调用兼容层、多账号

## 相对 9router PR #3921 的加固

- 自动导入 / 导入接口走 HTTP Basic admin，不再裸奔
- 不移植 `mimoEngine` 写 Desktop `tokens.json` 的路径（commit 2 后已无用且有副作用）
- 默认 `HOST=127.0.0.1`，CORS 不带 `credentials`
- 上游 OpenAI 直通，无需 `flattenContent` 这类 content-part 压平 hack
- 管理页只展示掩码，不回传完整 passToken / api_key

## 快速开始

```bash
pip install -r requirements.txt
# 确保已登录 MiMo Desktop 一次
python main.py
# 默认 http://127.0.0.1:8080
```

管理页 `/` → HTTP Basic `admin` / `config.json` 的 `admin_password` → **自动检测 → 导入**。

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
  -d '{"model":"mimo-x-flash-preview","messages":[{"role":"user","content":"hi"}]}'
```

## 凭证

| 字段 | 含义 |
|------|------|
| `mimo_pass_token` | Desktop cookie 库 `passToken`；SSO 续期与 Preview |
| `api_key` | `sk-` 官方 key；稳定模型走 `api.xiaomimimo.com` |
| 两者皆有 | Preview 走 Desktop，其余走官方 API |

`config.json` 已 gitignore，含账号会话，勿提交。

## 安全

- 默认只监听 `127.0.0.1`
- 暴露端口前改掉 `admin_password` / `api_keys`
- `passToken` 等价于账号登录态，按需在 OS 层加密落盘
