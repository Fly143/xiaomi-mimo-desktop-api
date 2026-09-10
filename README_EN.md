# xiaomi-mimo-desktop-api

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

Convert **Xiaomi MiMo Desktop account session** into **OpenAI + Anthropic compatible API**.

Desktop-exclusive models: `mimo-x-pro-preview`, `mimo-x-flash-preview`.

Based on [MiMo2API](https://github.com/Fly143/MiMo2API): same protocol layer (Chat / Responses / Anthropic Messages / tools / multi-account); upstream is the Desktop session instead of the web bot.

> 📖 中文文档见 [README.md](README.md)

## Features

- OpenAI `/v1/chat/completions`, `/v1/models`
- Anthropic `/v1/messages` (+ count_tokens, batches)
- Responses API `/v1/responses*`
- Function calling + stream sieve
- Multi-account rotation
- `passToken` → SSO → `serviceToken` (30 min cache, 401 retry)
- Fernet-encrypted credentials (`config.json` + `.secret_key`)

No TTS / ASR on this upstream.

## Quick start

```bash
pip install -r requirements.txt
# Sign in to MiMo Desktop once, then quit Desktop (cookie DB lock)
python main.py
# http://127.0.0.1:8080  (binds 0.0.0.0 by default; set HOST=127.0.0.1 for local-only)
```

Admin UI `/` — Basic auth `admin` / `admin_password` → **Detect → Import**.

```bash
curl -u admin:change-me http://127.0.0.1:8080/api/desktop/auto-import
curl -u admin:change-me -X POST http://127.0.0.1:8080/api/desktop/import \
  -H "Content-Type: application/json" \
  -d '{"mimoPassToken":"…","mimoUserId":"…","uid":"…"}'
```

## Call

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-x-pro-preview","messages":[{"role":"user","content":"hi"}]}'
```

Claude aliases: opus-class → `mimo-x-pro-preview`, sonnet/haiku → `mimo-x-flash-preview`.

## Environment

| Var | Default |
|-----|---------|
| `HOST` | `0.0.0.0` |
| `PORT` | `8080` |

## Security

- Back up `config.json` **and** `.secret_key` together
- Change `admin_password` / `api_keys` before exposing the port
- `passToken` is account login state — treat it as a secret

## License

MIT
