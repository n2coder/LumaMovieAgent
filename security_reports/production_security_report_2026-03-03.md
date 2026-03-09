# Production Security & Threat Testing Report

Date: 2026-03-03  
Assessor: Codex (Backend Security Review)  
Target: `d:\Projects\MovieRecomendationSystem`

## Scope

- FastAPI backend (`app/main.py`, `app/routes/*`, `app/services/*`, `app/models/*`)
- Static frontend (`app/static/index.html`, `app/static/script.js`)
- Deployment/config (`Dockerfile`, `render.yaml`, `.env.example`, `.gitignore`, `.dockerignore`, `requirements.txt`)
- Runtime abuse checks via `fastapi.testclient`

## Method

1. Static code and config review for common OWASP/API risks.
2. Dynamic endpoint abuse checks:
   - malformed input
   - oversized parameters
   - invalid session/audio flows
   - header hardening presence
3. Production readiness checks:
   - secret handling
   - authn/authz controls
   - resource exhaustion/cost-amplification vectors
   - deployment hardening gaps

## Executive Summary

Current build is **not production-ready** from a security perspective.  
Key blockers:

- Exposed OpenAI key in `.env.example` (critical secret leakage).
- No auth/rate limiting on expensive AI endpoints (high cost-abuse and DoS risk).
- Missing HTTP security headers and transport hardening defaults.
- Unbounded/weakly bounded resource controls on list endpoints and audio generation.
- Potential frontend injection surface via `insertAdjacentHTML` with unsanitized API data.

Risk rating: **High**  
Production decision: **Do not deploy publicly until critical/high findings are remediated.**

## Findings (Severity Ranked)

## 1) Critical: Secret material present in `.env.example`

- Evidence: `.env.example:7` contains a full `OPENAI_API_KEY` value.
- Impact:
  - Immediate key compromise if file is shared/committed/deployed.
  - Unauthorized API use and billing fraud.
- Recommendation:
  - Rotate/revoke exposed key immediately.
  - Replace `.env.example` with placeholder value only.
  - Add secret scanning in CI (e.g., `gitleaks` or `trufflehog`).
  - Enforce pre-commit secret checks.

## 2) High: No authentication/authorization on API endpoints

- Evidence:
  - Public endpoints: `/recommend`, `/voice-chat`, `/start-voice-session`, `/top-movies`, `/discover-movies`, `/poster-wall`.
  - No auth middleware or API key verification in `app/main.py` and routes.
- Impact:
  - Anyone can invoke costly OpenAI STT/TTS/LLM operations.
  - Abuse can cause account drain and service denial.
- Recommendation:
  - Require API key or JWT for all non-public endpoints.
  - Keep only `GET /health` public.
  - Add per-key quotas and revoke capability.

## 3) High: No rate limiting / abuse throttling on expensive routes

- Evidence:
  - No limiter middleware in app.
  - `/voice-chat` and `/recommend` can be called repeatedly without controls.
- Dynamic evidence:
  - Repeated invalid `/voice-chat` requests generated new TTS files (cost + storage growth).
- Impact:
  - Cost-amplification attack.
  - Resource exhaustion and degraded latency.
- Recommendation:
  - Add IP + API key rate limits (`slowapi`/gateway-level limits).
  - Separate strict limits for `/voice-chat` and TTS-heavy paths.
  - Add circuit-breaker/backpressure on OpenAI calls.

## 4) High: Resource exhaustion via unbounded query parameters

- Evidence:
  - `app/routes/recommend.py:34` `limit` is not capped.
  - `app/routes/recommend.py:48` `count` is not capped.
- Dynamic evidence:
  - `GET /top-movies?limit=1000000` returned 4735 records.
  - `GET /poster-wall?count=500000` returned 4720 URLs.
- Impact:
  - Large responses, memory pressure, high bandwidth, easy DoS vector.
- Recommendation:
  - Add strict validation (`Query(ge=1, le=50)` etc.).
  - Enforce max payload sizes globally at reverse proxy and app layer.

## 5) High: Missing HTTP security headers

- Dynamic evidence (`/`, `/health`, `/static/script.js`):
  - Missing: `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, `Strict-Transport-Security`, `Referrer-Policy`, `Permissions-Policy`.
- Impact:
  - Increased risk of XSS impact, clickjacking, MIME sniffing issues, weaker browser-side defenses.
- Recommendation:
  - Add security middleware for headers.
  - Enforce HTTPS in production and HSTS.

## 6) Medium: Potential XSS surface in frontend rendering path

- Evidence:
  - `app/static/script.js:82` uses `insertAdjacentHTML`.
  - `movie.title`, `movie.poster_url`, `movie.genres` are interpolated into HTML without escaping.
- Impact:
  - If movie data source is poisoned, this can lead to script injection in client.
- Recommendation:
  - Replace HTML string interpolation with safe DOM APIs (`textContent`, `setAttribute` with URL validation).
  - Validate/clean `poster_url` schemes (`https` only).

## 7) Medium: Sensitive internal errors may leak to users

- Evidence:
  - `app/services/llm_service.py:165` returns `LLM generation failed: {exc}` to client.
- Impact:
  - Information disclosure about upstream failures/internals.
- Recommendation:
  - Return generic error text to users.
  - Log detailed exception server-side with correlation IDs only.

## 8) Medium: Public API docs exposed in production by default

- Dynamic evidence:
  - `/docs`, `/redoc`, `/openapi.json` all returned `200`.
- Impact:
  - Easier endpoint reconnaissance for attackers.
- Recommendation:
  - Disable docs in production (`docs_url=None`, `redoc_url=None`, `openapi_url=None`) or protect behind auth.

## 9) Medium: Audio artifact retention and static exposure

- Evidence:
  - TTS writes persistent files under `app/static/audio` (`app/services/tts_service.py:20-30`).
  - No retention policy/cleanup job.
- Impact:
  - Disk growth over time, potential operational failure.
  - User content persistence risk.
- Recommendation:
  - Add TTL cleanup (scheduled job).
  - Use object storage with signed short-lived URLs.
  - Avoid storing unnecessary audio long-term.

## 10) Low: Input/file validation can be tightened

- Evidence:
  - `/voice-chat` checks filename existence but not explicit MIME allowlist or max size.
  - STT catches invalid audio and degrades gracefully (good), but pre-validation is minimal.
- Recommendation:
  - Enforce content-type allowlist.
  - Enforce max upload bytes and reject early.

## Positive Controls Observed

- `.gitignore` and `.dockerignore` exclude `.env`.
- Session IDs are UUID-based; session missing/expired handling exists.
- Short-memory window implemented for lower token footprint.
- Basic health endpoint exists for orchestration checks.

## Dynamic Test Results Snapshot

- `GET /health` -> `200`.
- `POST /recommend` with empty query -> `400` (expected).
- `POST /recommend` huge query -> `200` (not blocked; should enforce max length).
- `GET /top-movies?limit=1000000` -> `200`, 4735 items (unbounded).
- `GET /discover-movies?limit=-10` -> `200`, coerced to 1 (works but should validate formally).
- `GET /poster-wall?count=500000` -> `200`, 4720 items (unbounded).
- `POST /voice-chat` invalid audio -> `200` repeat prompt + TTS file generation.
- `POST /voice-chat` invalid session -> `404` (expected).

## Dependency/Vulnerability Scanning Status

- `pip-audit`: not available in environment.
- `bandit`: not installed in environment.
- Recommendation:
  - Add CI jobs for `pip-audit` and `bandit`.
  - Add Software Bill of Materials (SBOM) generation and dependency monitoring.

## Production Hardening Checklist (Priority Order)

1. Rotate leaked OpenAI key immediately and purge from all files/history.
2. Add authentication for AI endpoints and enforce per-client quotas.
3. Add rate limiting and request-size limits at gateway + FastAPI.
4. Add strict query validation caps for `limit`/`count`.
5. Add security headers + HTTPS redirect/HSTS in production.
6. Remove raw exception leakage to clients.
7. Disable or protect API docs in production.
8. Replace unsafe HTML rendering path in frontend with safe DOM API.
9. Add audio retention cleanup and storage limits.
10. Add automated security testing in CI/CD.

## Suggested Threat Model (STRIDE quick map)

- Spoofing: No auth on key endpoints.
- Tampering: Data-source poisoning can affect rendered UI content.
- Repudiation: Limited structured security logging/correlation IDs.
- Information Disclosure: Error leakage + possible exposed secrets.
- Denial of Service: Unbounded list params, no rate limits, expensive endpoint abuse.
- Elevation of Privilege: Not directly observed, but public surface is broad.

## Final Assessment

This system can run functionally, but for internet-facing production it currently has multiple high-risk controls missing.  
Implement the top 5 checklist items before public deployment.

