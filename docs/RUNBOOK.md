# Runbook

## Deploy
- **Worker**: push to branch `bmediahub-worker` → Render auto-deploys. Check `GET /` on the worker (`ok:true`, `warp:true`).
- **Vercel**: deploy the files under `telegram-vercel/src` with project root dir = `src` (paths: `api/index.js`, `lib/*.js`, `package.json`, `vercel.json`). Verify `GET /api/health` (`redisConfigured:true`), then send `/start` to the bot.
- After the first deploy, open `https://<vercel-domain>/api/activate` only if the webhook was lost.

## Rollback
Vercel: promote the previous deployment (dashboard → Deployments → ⋯ → Promote / Instant Rollback). Worker: Render → Deploys → Rollback. Quotas keep working across rollbacks (data lives in Redis).

## First-time setup after deploying the quota layer
1. Create the channels manually (bots cannot create channels), add the bot as admin in each.
2. In the bot (as admin): `/setup`, `/setmain @main_channel`, `/addchannel @bonus_channel` (repeat).
3. Test with a second account: terms → subscribe → link → credit decrements (`/me`).

## Moving to your own server (VPS)
`docker compose -f mediahub-worker/docker-compose.yml up -d --build` from the repo root (set the env vars in `.env`). Then change Vercel's `MEDIA_WORKER_URL` to the new URL and set `USAGE_URL` on the server. The webhook can later move to Cloudflare Workers; the Redis data is portable.

## Emergency switches
`/maint on` (blocks users), `/ban ID`, `/limit 1`. If Redis misbehaves the bot fails open — set `ALLOWED_USER_IDS` on Vercel to lock the bot to specific users.
