# Render Deployment Audit (Security + Code Quality)

Date: 2026-03-09  
Target: `d:\Projects\MovieRecomendationSystem`

## Verdict

Conditional go-live. Core blockers were addressed in this pass, but final Render env configuration is still required.

## Changes Applied

1. Production secret enforcement:
   - Startup now fails in production if `SESSION_JWT_SECRET` is weak.
2. Host allowlist improvement:
   - `ALLOWED_HOSTS` now auto-includes `RENDER_EXTERNAL_HOSTNAME`.
3. WebSocket abuse hardening:
   - Added payload size cap and per-IP `user_query` rate limit.
4. Storage hardening:
   - Added automatic TTS audio retention cleanup.
5. Container hardening:
   - Removed duplicate installs, switched to non-root user, set default `WEB_CONCURRENCY=1`.
6. Frontend/API-key interoperability:
   - Frontend now supports `?api_key=...` for `/recommend` and `/ws/voice`.
7. Docker context hygiene:
   - Excluded generated MP3 files from Docker build context.

## Remaining Risks / Required Deployment Steps

1. Set Render secrets:
   - `OPENAI_API_KEY` (required)
   - `SESSION_JWT_SECRET` (auto-generated in `render.yaml`, but verify)
2. Verify `ALLOWED_HOSTS`:
   - Include actual Render service/custom domain.
3. Decide API key strategy:
   - If `APP_API_KEY` is enabled for browser usage, launch URL must include `?api_key=<value>`.
4. Keep `WEB_CONCURRENCY=1` on small instances due model memory footprint.
5. Add CI security scanning:
   - `pip-audit`, `bandit`, and secret scanning.

## Files Touched for Hardening

- `app/main.py`
- `app/config.py`
- `app/services/tts_service.py`
- `app/static/script.js`
- `Dockerfile`
- `.dockerignore`
- `render.yaml`
- `README.md`
