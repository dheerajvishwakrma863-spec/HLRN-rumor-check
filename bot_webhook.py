"""
bot_webhook.py - Chat-bot front door for HLRN (WhatsApp Cloud API + Telegram + a demo endpoint).

A user forwards a rumour to the bot; the webhook passes the text through the SAME engine the
dashboard uses (cache -> Layer 1 -> Layer 2 -> guardrails) and sends the verdict back in the
user's own language. This proves the engine is channel-agnostic: the dashboard is just one client.

Run
---
    uvicorn bot_webhook:app --port 8000

Try it without any bot credentials (mock mode)
----------------------------------------------
    curl -s localhost:8000/simulate -H "Content-Type: application/json" \
         -d '{"text": "*Forwarded many times* Sarkar sabhi students ko free laptop de rahi hai ...", "user_id": "demo"}'

Endpoints
---------
GET  /health                    liveness probe
POST /simulate                  JSON in -> JSON verdict + the chat reply text (for demos / tests)
POST /telegram/webhook          Telegram update; replies inline in the HTTP response (no token needed)
GET  /whatsapp/webhook          Meta verification handshake
POST /whatsapp/webhook          WhatsApp Cloud API messages; replies via the Graph API if credentials
                                are set, otherwise the reply is only logged (mock mode)

Hook-up cheat-sheet
-------------------
Telegram : create a bot with @BotFather, expose this server over HTTPS (e.g. `ngrok http 8000`), then
           https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<host>/telegram/webhook&secret_token=<SECRET>
           and set TELEGRAM_WEBHOOK_SECRET=<SECRET>.
WhatsApp : Meta for Developers -> WhatsApp -> Configuration: callback URL https://<host>/whatsapp/webhook,
           verify token = WHATSAPP_VERIFY_TOKEN; set WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID and
           WHATSAPP_APP_SECRET (enables signature checking).

Limitations (MVP): text only (media would be downloaded and passed as `media_bytes`), and this process
has its own memory cache, so a moderator override made in the dashboard reaches the bot immediately via
the shared database but cached answers may linger up to CACHE_TTL_SEC.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from config import settings
from engine import (
    VERDICT_FAKE, VERDICT_REVIEW, VERDICT_TRUE, HLRNEngine, MediaError, RateLimitExceeded, TTLCache,
    VerificationResult,
)

log = logging.getLogger("hlrn.bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TELEGRAM_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
WA_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "hlrn-verify")
WA_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WA_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WA_PHONE_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
MAX_REPLY = int(os.getenv("BOT_MAX_REPLY_CHARS", "1500"))

app = FastAPI(title="HLRN Bot Webhook", version="1.0")
_engine: Optional[HLRNEngine] = None
_seen_msgs = TTLCache(ttl_sec=900, max_items=20000)      # Meta retries deliveries: de-duplicate by message id

WELCOME = (
    "🛡️ HLRN Rumor Check\n\n"
    "Forward me any suspicious message (Hindi / Hinglish / English) and I'll check it against verified "
    "records and official sources before you share it."
)


def get_engine() -> HLRNEngine:
    """Lazy singleton so importing this module (tests, uvicorn --reload) stays cheap."""
    global _engine
    if _engine is None:
        _engine = HLRNEngine(settings)
    return _engine


# --------------------------------------------------------------------------- reply formatting
def format_chat_reply(res: VerificationResult) -> str:
    """Compact plain-text reply (no markup, so it renders identically on WhatsApp and Telegram)."""
    icon = {VERDICT_TRUE: "✅", VERDICT_FAKE: "🚫", VERDICT_REVIEW: "🕵️"}[res.verdict]
    lines = [f"{icon} {res.verdict} ({res.confidence_score:.0f}%)", "", res.neutral_explanation.strip()]
    if res.recommended_action.strip():
        lines += ["", "👉 " + res.recommended_action.strip()]
    links = [s.url for s in res.official_sources if s.url.lower().startswith(("http://", "https://"))][:2]
    if links:
        lines += ["", "Sources:"] + [f"• {u}" for u in links]
    origin = "verified database" if res.served_from in ("cache", "database") else "live source check"
    lines += ["", f"— HLRN automated check ({origin}). Not an official authority."]
    text = "\n".join(lines)
    return text if len(text) <= MAX_REPLY else text[: MAX_REPLY - 1].rstrip() + "…"


async def run_check(text: str, client_id: str) -> Tuple[str, Optional[VerificationResult]]:
    """Shared by every channel. Never raises: always returns something safe to send to a human."""
    try:
        res = await run_in_threadpool(get_engine().verify, text, client_id=client_id, session_id=client_id)
        return format_chat_reply(res), res
    except RateLimitExceeded as e:
        return f"You're sending messages very quickly. Please try again in about {e.retry_after} seconds.", None
    except (ValueError, MediaError) as e:
        return str(e), None
    except Exception:  # noqa: BLE001
        log.exception("verification failed")
        return "Sorry, something went wrong on our side. Please try again in a moment.", None


# --------------------------------------------------------------------------- /health & /simulate
@app.get("/health")
def health() -> Dict[str, Any]:
    eng = get_engine()
    return {"status": "ok", "ai_enabled": eng.llm.enabled, "cache_entries": len(eng.cache)}


class SimulateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000, description="The forwarded message")
    user_id: str = Field("demo-user", max_length=64)
    channel: Literal["whatsapp", "telegram"] = "whatsapp"


@app.post("/simulate")
async def simulate(body: SimulateRequest) -> Dict[str, Any]:
    """Mock a chat message end-to-end. Returns the chat reply AND the full structured verdict."""
    t0 = time.perf_counter()
    reply, res = await run_check(body.text, f"{body.channel[:2]}:{body.user_id}")
    if res is None:
        raise HTTPException(status_code=429 if "quickly" in reply else 422, detail=reply)
    return {
        "channel": body.channel,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "reply": reply,
        "result": res.model_dump(exclude={"internal_model_verdict"}),   # moderator-only field stays private
    }


# --------------------------------------------------------------------------- Telegram
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    if TELEGRAM_SECRET and not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), TELEGRAM_SECRET
    ):
        raise HTTPException(status_code=403, detail="bad secret")
    update = await request.json()
    msg = update.get("message") or update.get("edited_message")
    if not msg or "chat" not in msg:
        return {"ok": True}                                    # ignore joins, reactions, etc.

    chat_id, text = msg["chat"]["id"], (msg.get("text") or msg.get("caption") or "").strip()
    if text.startswith("/start") or text.startswith("/help"):
        reply = WELCOME
    elif not text:
        reply = "Please send the message text you'd like me to check (media support is coming soon)."
    else:
        reply, _ = await run_check(text, f"tg:{chat_id}")
    # Telegram lets a webhook answer directly with the API method to execute - no bot token needed.
    return {"method": "sendMessage", "chat_id": chat_id, "text": reply,
            "reply_to_message_id": msg.get("message_id"), "disable_web_page_preview": True}


# --------------------------------------------------------------------------- WhatsApp Cloud API
@app.get("/whatsapp/webhook")
def whatsapp_verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    """Meta calls this once when you register the webhook."""
    if mode == "subscribe" and hmac.compare_digest(token, WA_VERIFY_TOKEN):
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


def _valid_meta_signature(raw: bytes, header: str) -> bool:
    if not WA_APP_SECRET:                                      # mock/dev mode: signature checking off
        return True
    expected = "sha256=" + hmac.new(WA_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _extract_wa_texts(payload: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Return [(message_id, sender_phone, text)] from a WhatsApp Cloud API payload."""
    out: List[Tuple[str, str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []) or []:
                if m.get("type") == "text":
                    out.append((m.get("id", ""), m.get("from", ""), m["text"].get("body", "")))
    return out


def send_whatsapp(to: str, body: str) -> None:
    if not (WA_TOKEN and WA_PHONE_ID):
        log.info("[mock mode] WhatsApp reply to %s:\n%s", to[-4:].rjust(len(to), "*"), body)
        return
    try:
        r = requests.post(
            f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text",
                  "text": {"body": body, "preview_url": False}},
            timeout=10,
        )
        if r.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", r.status_code, r.text[:300])
    except requests.RequestException:
        log.exception("WhatsApp send error")


async def _handle_wa_message(sender: str, text: str) -> None:
    reply, _ = await run_check(text, f"wa:{sender}")
    await run_in_threadpool(send_whatsapp, sender, reply)


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request, background: BackgroundTasks) -> Dict[str, str]:
    raw = await request.body()
    if not _valid_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json")
    for msg_id, sender, text in _extract_wa_texts(payload):
        if msg_id and _seen_msgs.get(msg_id):
            continue                                            # duplicate delivery from Meta
        _seen_msgs.set(msg_id, True)
        # Meta requires a fast 200; the (possibly slow) AI check runs in the background.
        background.add_task(_handle_wa_message, sender, text.strip() or "(empty)")
    return {"status": "received"}
