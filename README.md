# alfursan-wholesale-bot (merged service)

This service runs **4 bots** as supervised child processes behind a single
Render web service, replacing 4 separate Render services:

| Process       | Language | What it does                                             |
|---------------|----------|-----------------------------------------------------------|
| `wholesale`   | Node.js  | Syncs 4 Telegram channels to the wholesale website         |
| `price`       | Python   | Stamps a price box onto product photos sent by Telegram    |
| `promo`       | Python   | Meta Ads performance reports on Telegram (`/report`)        |
| `order-status`| Node.js  | WhatsApp bot answering order-status lookups                |

`orchestrator.js` is the single process Render starts. It:
- binds the public `$PORT` and answers Render's health check at `/`
  with a JSON status of all 4 bots,
- reverse-proxies `GET /whatsapp` to the order-status bot so its QR
  login page stays reachable through the one public URL,
- spawns each bot with its own **internal** port (10001–10004) so
  they never collide on `$PORT`,
- restarts any bot that crashes, with increasing backoff,
- forwards `SIGTERM`/`SIGINT` to all children for a clean shutdown.

## Why Docker

The bots mix Node.js and Python. Render's native runtime only supports one
language per service, so this service is deployed as a **Docker** web
service (see `Dockerfile`) built on `python:3.12-slim` with Node.js 20
installed on top.

## Environment variables

Some bots originally used the *same* variable name (e.g. both `wholesale`
and `price` used `BOT_TOKEN` for two different Telegram bots). To avoid
collisions when running in one process tree, set these **renamed** variables
on the Render service; the orchestrator maps them to the name each bot
actually expects internally.

| Set this on Render          | Used by        | Internally becomes |
|------------------------------|-----------------|---------------------|
| `WHOLESALE_BOT_TOKEN`         | wholesale        | `BOT_TOKEN`          |
| `PRICE_BOT_TOKEN`             | price            | `BOT_TOKEN`          |
| `PROMO_TELEGRAM_BOT_TOKEN`    | promo            | `TELEGRAM_BOT_TOKEN` |
| `WHOLESALE_GEMINI_API_KEY`    | wholesale (optional, falls back to `GEMINI_API_KEY`) | `GEMINI_API_KEY` |
| `PROMO_GEMINI_API_KEY`        | promo (optional, falls back to `GEMINI_API_KEY`)     | `GEMINI_API_KEY` |
| `GEMINI_API_KEY`              | shared fallback for the two above if you're fine reusing one key | — |
| `META_ACCESS_TOKEN`           | promo            | same                 |
| `META_AD_ACCOUNT_ID`          | promo (optional) | same                 |
| `TARGET_CHAT_ID`              | promo (optional) | same                 |
| `REPORT_INTERVAL_HOURS`       | promo (optional) | same                 |
| `GEMINI_MODEL`                | promo (optional) | same                 |
| `AUTH_STATE_DIR`              | order-status (WhatsApp session folder — see below) | same |

## Persistent WhatsApp session

The order-status bot stores its WhatsApp login under `AUTH_STATE_DIR`.
Without a persistent disk, that folder is wiped on every deploy/restart and
you'd have to re-scan the QR code each time. This service is configured
(see `render.yaml`) with a **Render Persistent Disk** mounted at `/data`,
with `AUTH_STATE_DIR=/data/auth_state`.

To scan the QR code after first deploy, open:
`https://<your-render-url>/whatsapp`

## Deploying on Render

1. Change the existing service's environment from "Node" to **Docker**
   (Render → service → Settings → Build & Deploy → Environment), or create
   a new service and point it at this repo/branch with `runtime: docker`.
2. Add a **Persistent Disk**: 1GB mounted at `/data` (Settings → Disks).
3. Set all env vars listed above (Settings → Environment).
4. Deploy, then open `/whatsapp` once to scan the WhatsApp QR code.
5. Confirm `/` returns `"alive": true` for all 4 bots.
6. Once verified, cancel/pause the 3 old standalone Render services for
   price-bot, promo-bot and order-status-bot to stop paying for them.

## Local Docker test

```bash
docker build -t alfursan-merged .
docker run --rm -p 10000:10000 \
  -e WHOLESALE_BOT_TOKEN=... \
  -e PRICE_BOT_TOKEN=... \
  -e PROMO_TELEGRAM_BOT_TOKEN=... \
  -e META_ACCESS_TOKEN=... \
  alfursan-merged
curl localhost:10000/
```
