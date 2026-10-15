# Media hub saver (@BMEDIAHUB_BOT) — project overview

Telegram media downloader bot. **Telegram → Vercel webhook (Node) → Render worker (Python/FastAPI/yt-dlp/FFmpeg/WARP)**, with Upstash Redis for quotas.

| Part | Folder | Runs on | Branch (deploys from) |
|---|---|---|---|
| Webhook, quotas, gating, admin | `telegram-vercel/src` (Vercel root dir = `src`) | Vercel (deployed directly via MCP/CLI, no git link) | `dev-vercel` |
| Media worker | `mediahub-worker/` (Docker build context = repo root) | Render free Docker service `bmediahub-worker` | `bmediahub-worker` (auto-deploy) |

## Environment variables (names only — never commit values)
**Vercel (`bmediahub-telegram`)**: `TG_BOT_TOKEN`, `TG_WEBHOOK_SECRET`, `MEDIA_WORKER_URL`, `MEDIA_WORKER_SECRET`, `TG_PUBLIC_WEBHOOK_URL`, `ADMIN_USER_IDS` (comma list, default `5375428860`), Upstash: `KV_REST_API_URL` + `KV_REST_API_TOKEN` (or `UPSTASH_REDIS_REST_URL`/`_TOKEN`). Optional `ALLOWED_USER_IDS` (allowlist; leave empty to keep the bot open).
**Render (`bmediahub-worker`)**: `TG_BOT_TOKEN`, `MEDIA_WORKER_SECRET` (same value as Vercel), `DOWNLOAD_SIGNING_SECRET`, `WARP_ENABLED=1`; optional `USAGE_URL` (default = the Vercel `/api/usage`), `DAILY_LIMIT` (safety net, default 100000), `IG_COOKIES*` (unused).

## Usage rules (enforced on Vercel, stored in Redis, Cairo time)
- Must accept terms, then subscribe to the **main channel** (`/setmain @channel`, bot must be admin there).
- Welcome gift: 5 downloads (never expires). From the next Cairo day: 2 downloads/day.
- Video = 3 units (= 1 download); all tools on the same video are free after the first delivery. Photo action = 1 unit (3 = 1 download). Cover / caption are free.
- When exhausted: joining a *new* bonus channel (`/addchannel @x`) = +5 downloads for the rest of that day, once per day.
- Credit is charged **after** a successful delivery (worker → `POST /api/usage`). Redis down ⇒ fail-open (nobody is locked out).

## Admin (Telegram, only IDs in `ADMIN_USER_IDS`)
`/admin` panel · `/setup` (sets bot name, descriptions, command menus) · `/user ID` · `/grant ID N` · `/ban` `/unban ID` · `/unlimited ID on|off` · `/limit N` · `/welcome N` · `/bonus N` · `/maint on|off` · `/channels` `/addchannel` `/delchannel` `/setmain` · `/broadcast text` · `/bw` · `/stats`.

## Free-tier limits to watch
- **Render outbound bandwidth 5 GB/month** (the real cap; only data sent to Telegram counts). The bot alerts the admin at 4.5 GB and 5 GB. Overage ≈ $0.15/GB (card on file).
- Render free: 750 h/month (one always-on service), sleeps after 15 min idle (~1 min wake — the bot wakes it automatically), ephemeral disk (files deleted after 2 h).
- Upstash free: 500K commands/month (~20 per download ⇒ ~25K downloads).
- Vercel Hobby: non-commercial use only.
