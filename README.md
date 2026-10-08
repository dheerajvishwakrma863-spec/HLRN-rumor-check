# HLRN — Hybrid Local Rumor Neutralizer (MVP)

Layered rumor-verification system: **instant verified-database answers first, live grounded AI only when needed, human moderators close the loop.**

```
 user ─► rate limit ─► preprocess (noise strip, Hinglish-safe normalise, keyframes)
                          │
                          ├─► TTL memory cache ──────────────┐  0 tokens
                          ├─► Layer 1: SQLite exact → fuzzy ─┤  0 tokens   (admin-verified wins)
                          └─► Layer 2: Gemini + Search Grounding (single-flight, budgeted)
                                   └─► Guardrails ─► DB + cache ─► PII-redacted audit log
 moderators ─► Admin panel ─► override / blacklist ─► DB updated + cache flushed instantly
```

## Files
| File | Purpose |
|---|---|
| `preprocessing.py` | Forward-noise stripping, emoji flood control, Hinglish-safe normalisation, language/script detection, fuzzy similarity + safety guards, PII redaction |
| `database.py` | SQLite (WAL): claims/blacklist, inverted token index, translations, audit logs, admin actions |
| `engine.py` | TTL cache, rate limiter, single-flight lock, OpenCV keyframes, Gemini grounding, Pydantic schemas, guardrails, orchestrator + worker pool |
| `app.py` | Streamlit UI: Verify tab (async job + live progress) and password-protected Moderator Panel |
| `config.py` | All tunables, read from `.env` |
| `tests/test_core.py` | Offline tests (no network) |

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then edit: GEMINI_API_KEY, ADMIN_PASSWORD, AUDIT_HASH_SALT
streamlit run app.py
pytest -q                                               # optional: run offline tests
```
Get a key at https://aistudio.google.com/apikey. Without a key the app still runs (Layer 1 + moderator panel); Layer 2 returns "Needs Human Review".

## How each requirement is met
- **Noise stripping / code-mixed text** – `preprocess()` returns `cleaned` (for the LLM: wording, slang and script untouched) and `normalized` (for hashing only: lowercase, emoji/punctuation removed, Devanagari digits → ASCII, `₹`/`rs`/`inr` unified, Hinglish spelling variants like `nahin/nhi → nahi` unified, tracking params stripped from URLs). No words are translated, stemmed or deleted. Extend `_VARIANTS` in `preprocessing.py` with your region's slang.
- **Exact + fuzzy match** – SHA-256 of the normalized text, then an indexed candidate fetch + Jaccard/sequence blend. **Safety guards:** two messages never fuzzy-match if their numbers or negation words differ (“is true” vs “is not true”, “₹500” vs “₹5000”, a different link). AI-only records need ≥0.90 similarity; admin-verified ones use `FUZZY_THRESHOLD`.
- **Zero-token caching** – in-memory LRU+TTL cache keyed by content + language; single-flight lock so 500 simultaneous copies of a viral forward cost **one** LLM call.
- **Keyframes** – OpenCV samples ~4× candidates, keeps only visually distinct, non-black frames (≤ `MAX_KEYFRAMES`), downsizes to `FRAME_MAX_SIDE` (768 px ≈ one image tile) and sends JPEGs instead of the video stream. Images are decompression-bomb checked. Exact-duplicate media is matched by SHA-256; near-duplicate **images** by a 256-bit dHash (+ same caption).
- **Live grounding** – `GoogleSearch` tool on the Gemini call. `official_sources` come from the API's grounding metadata (never from model-typed URLs), Google redirect links are resolved to the real publisher URL, and each source is tagged `official` / `fact_checker` / `other`.
- **Multilingual** – the language/script of the input (English, Hinglish, Devanagari, Bengali, Tamil…) is detected and enforced in the prompt. Layer-1 hits in a different language are translated **once** per (claim, language) and cached.
- **Structured output** – Pydantic `VerificationResult` (all required fields + metadata). Grounded calls cannot enforce a response schema, so the JSON is requested in the prompt, parsed tolerantly, and a cheap no-tools repair call with `response_schema` is used if parsing fails.
- **Guardrails** – (R1) confidence < 75 ⇒ never “Fake”/“True” → *Needs Human Review*; (R2) sensitive categories (communal, politics) need ≥ 90; (R3) a definitive verdict needs ≥ 1 official/fact-checker source. Withheld verdicts get a neutral localized message and the model's leaning is stored for moderators only. Prompt-injection hardening: user text is passed as delimited untrusted data.
- **PII** – phones, e-mail, UPI, Aadhaar, PAN, card numbers, OTPs, IPs and heuristic names are masked **before** audit logging and moderator samples. Raw client IPs are never stored (salted hash only). Logs auto-purge after `AUDIT_RETENTION_DAYS`.
- **Rate limiting** – per-client request window, per-client hourly AI quota, global daily AI budget, bounded job queue, upload/duration/pixel limits.
- **Non-blocking UI** – work runs in a bounded `ThreadPoolExecutor`; the page polls with a live progress bar and the job survives reruns.
- **Admin feedback loop** – review queue (AI abstentions + user-flagged answers, most-forwarded first), override/revoke, manual blacklist entries, full audit trail. Admin records are permanent and can never be overwritten by the AI. Every action flushes the cache.

## Operating notes / known limits (please read)
1. **Verified against a stub, not the live APIs.** Offline logic (preprocessing, DB, fuzzy guards, guardrails, cache, limiter, keyframes) is tested. The Gemini SDK call shapes (`google-genai` ≥ 1.20) and the Streamlit UI should be smoke-tested once with your key; pin versions after that.
2. **Video audio is not transcribed.** Spoken claims with no on-screen text are missed. Add an ASR step (e.g. Whisper) before Layer 2 if needed.
3. **Rate limiter / cache are per-process.** For several instances use Redis for both, and Postgres instead of SQLite.
4. **Client identity** uses `X-Forwarded-For` only if your reverse proxy sets it; otherwise it falls back to the browser session (easy to evade). Put Cloudflare/nginx rate limiting in front for real DDoS protection.
5. **Name redaction is heuristic.** For regulated use plug Presidio/spaCy into `redact_pii()`.
6. **Search grounding can't be domain-restricted** in the API; the prompt prefers official sources and rule R3 enforces them after the fact. Extend `EXTRA_TRUSTED_DOMAINS` for state portals.
7. Not legal advice: have counsel review the disclaimer text and your moderator process before public launch.

## Production checklist
HTTPS + reverse proxy · secrets via a vault · Postgres/Redis · move the worker pool to a queue (Celery/RQ) for very high load · scheduled DB backups · moderator SSO instead of a shared password · monitoring on `ai_calls_24h` / `tokens_24h` (Ops tab).

## Demo & deployment extras
| File | Purpose |
|---|---|
| `seed_data.py` | Loads 14 demo rumours (Hindi/Hinglish/English) as admin-verified Layer-1 records and self-tests them. `--fresh` wipes the DB first. |
| `run.sh` / `run.bat` | One-click: venv → `pip install` → seed → `streamlit run app.py` |
| `Dockerfile`, `docker-compose.yml` | `docker compose up --build` (dashboard :8501). `docker compose --profile bot up` also starts the webhook on :8000. Data persists in the `hlrn-data` volume. |
| `bot_webhook.py` | FastAPI: `/simulate`, `/telegram/webhook`, `/whatsapp/webhook`. Runs in mock mode without credentials. |
| `PITCH_GUIDE.md` | 5-slide outline, 2-minute demo script with exact snippets, judge Q&A |

The database file defaults to `data/hlrn.db` (`HLRN_DB_PATH`). Seeded entries are demo content: verify them against current official sources before presenting them as real fact-checks.

## Choosing / fixing the Gemini model
`python model_doctor.py` lists the models your key can use, tests one (including Search grounding) and prints the exact `GEMINI_MODEL=...` line for `.env`. The app never hardcodes a model: it uses `GEMINI_MODEL`, then `GEMINI_FALLBACK_MODELS`, then auto-discovery, and switches automatically if a model returns 404 (retired) or 400 (wrong type, e.g. Live/audio models). `.env` overrides stale shell variables. Always restart Streamlit after editing `.env`.
