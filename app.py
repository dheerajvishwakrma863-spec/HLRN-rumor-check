"""
app.py - HLRN Streamlit front-end.

  * "Verify" tab        : paste text and/or upload image/video. Work runs in a bounded worker
                          pool, the UI polls with a live progress bar, so slow video decoding or
                          LLM calls never freeze the page (other users' sessions are unaffected).
  * "Moderator Panel"   : password-protected review queue. Overrides are written to the DB and
                          the memory cache is flushed, so the very next identical forward gets
                          the corrected verdict at zero token cost (self-improving loop).

Run:  streamlit run app.py
"""
from __future__ import annotations

import hmac
import logging
import time
import uuid
from datetime import datetime

import streamlit as st

from config import settings
from database import STATUS_ADMIN, STATUS_AI, STATUS_PENDING, STATUS_REVOKED
from engine import (
    CATEGORIES, VERDICT_FAKE, VERDICT_REVIEW, VERDICT_TRUE, VERDICTS, HLRNEngine, MediaError,
    RateLimitExceeded, ServerBusy, VerificationResult,
)
from preprocessing import LANGUAGE_LABELS, preprocess, redact_pii, truncate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
st.set_page_config(page_title="HLRN - Rumor Check", page_icon="🛡️", layout="wide")

LANG_TAGS = [k for k in LANGUAGE_LABELS if k != "mixed"]
REVOKE = "⛔ Revoke entry (stop serving it)"


# --------------------------------------------------------------------------- shared resources
@st.cache_resource(show_spinner="Starting HLRN engine…")
def get_engine() -> HLRNEngine:
    """One engine per server process: shared cache, rate limiter, worker pool and DB."""
    return HLRNEngine(settings)


engine = get_engine()
if "sid" not in st.session_state:
    st.session_state.sid = uuid.uuid4().hex[:12]


def client_id() -> str:
    """
    Best available client identity. Behind a trusted reverse proxy (nginx / Cloudflare) the
    forwarded IP is used; otherwise we fall back to the browser session.
    NOTE: X-Forwarded-For is only trustworthy if YOUR proxy sets/overwrites it.
    """
    try:
        h = st.context.headers
        ip = h.get("X-Forwarded-For") or h.get("X-Real-Ip")
        if ip:
            return "ip:" + ip.split(",")[0].strip()
    except Exception:  # noqa: BLE001 - older Streamlit versions have no st.context
        pass
    return "sess:" + st.session_state.sid


# --------------------------------------------------------------------------- rendering helpers
def _md_safe(s: str) -> str:
    return s.replace("[", "(").replace("]", ")")


def render_result(res: VerificationResult) -> None:
    box = {VERDICT_TRUE: st.success, VERDICT_FAKE: st.error, VERDICT_REVIEW: st.warning}[res.verdict]
    icon = {VERDICT_TRUE: "✅", VERDICT_FAKE: "🚫", VERDICT_REVIEW: "🕵️"}[res.verdict]
    with st.container(border=True):
        box(f"### {icon} {res.verdict}")
        c1, c2, c3 = st.columns(3)
        c1.metric("Confidence", f"{res.confidence_score:.0f}%")
        c2.metric("Category", res.matched_category)
        c3.metric("Checked by", res.source_layer)
        st.progress(min(max(res.confidence_score / 100, 0.0), 1.0))

        if res.served_from in ("cache", "database"):
            who = "a human moderator" if res.human_verified else "our verified database"
            st.caption(f"⚡ Answered instantly from {who} — 0 AI tokens used.")
        elif res.served_from == "ai":
            st.caption(f"🔎 Checked live against web sources · {res.tokens_used:,} tokens used.")

        st.markdown("#### What we found")
        st.write(res.neutral_explanation)
        st.markdown("#### What you can do")
        st.write(res.recommended_action)

        if res.supporting_evidence:
            with st.expander("Supporting evidence", expanded=True):
                for e in res.supporting_evidence:
                    st.markdown(f"- {e}")
        if res.official_sources:
            with st.expander("Sources", expanded=True):
                badge = {"official": "🏛️ Official", "fact_checker": "✅ Fact-checker", "other": "🌐 Web"}
                for s in res.official_sources:
                    if s.url.lower().startswith(("http://", "https://")):
                        st.markdown(f"- {badge[s.trust_tier]} · [{_md_safe(s.title)}]({s.url})")
        for n in res.guardrail_notes:
            st.caption(f"🛡️ {n}")

    st.caption("HLRN is an automated aid, not a legal or official authority. Always confirm important "
               "information with the relevant government department.")
    if res.claim_id is not None:
        flagged = st.session_state.setdefault("flagged", set())
        if res.claim_id in flagged:
            st.caption("Thanks — this has been sent to our moderators.")
        elif st.button("🚩 This answer looks wrong — send for human review", key=f"flag{res.claim_id}"):
            engine.db.flag_claim(res.claim_id)
            flagged.add(res.claim_id)
            st.rerun()


# --------------------------------------------------------------------------- Verify tab
def verify_tab() -> None:
    st.subheader("Check a forwarded message, image or video")
    with st.form("verify_form", clear_on_submit=False):
        text = st.text_area("Paste the message (any language — Hindi, Hinglish, English…)", height=170,
                            max_chars=settings.max_input_chars * 2,
                            placeholder="e.g. Sarkar sabko free laptop de rahi hai, jaldi register karo…")
        upload = st.file_uploader(
            f"…or upload a screenshot / video (max {settings.max_upload_mb} MB)",
            type=["jpg", "jpeg", "png", "webp", "mp4", "mov", "webm", "mkv", "avi", "m4v"],
        )
        go = st.form_submit_button("🔍 Verify", type="primary", use_container_width=True)

    if go:
        st.session_state.result = None
        data = upload.getvalue() if upload else None
        if data and len(data) > settings.max_upload_mb * 1024 * 1024:
            st.error(f"File is larger than {settings.max_upload_mb} MB.")
        elif not (text or "").strip() and not data:
            st.warning("Please paste a message or upload a file first.")
        else:
            try:
                st.session_state.job = engine.submit(
                    text or "", media_bytes=data, media_name=upload.name if upload else "",
                    client_id=client_id(), session_id=st.session_state.sid)
            except RateLimitExceeded as e:
                st.warning(f"You're sending requests very quickly. Please wait {e.retry_after}s and try again.")
            except ServerBusy as e:
                st.warning(str(e))

    # ---- non-blocking progress polling (the job survives reruns in session_state) ----
    job = st.session_state.get("job")
    if job is not None:
        with st.status("Verifying…", expanded=True) as status:
            bar = st.progress(0)
            deadline = time.time() + 120
            while not job.future.done() and time.time() < deadline:
                bar.progress(min(job.progress.pct, 99) / 100, text=job.progress.stage)
                time.sleep(0.4)
            if not job.future.done():
                status.update(label="Still working… this is taking longer than usual.", state="running")
                st.info("Heavy video? You can keep this page open; press Verify again to re-check shortly.")
            else:
                st.session_state.job = None
                try:
                    st.session_state.result = job.future.result()
                    status.update(label="Done", state="complete", expanded=False)
                except RateLimitExceeded as e:
                    status.update(label="Limit reached", state="error")
                    st.warning(f"AI-check limit reached ({e.scope}). Please try again in about {e.retry_after}s.")
                except (MediaError, ValueError) as e:
                    status.update(label="Cannot process input", state="error")
                    st.error(str(e))
                except Exception:  # noqa: BLE001
                    logging.getLogger("hlrn").exception("verification failed")
                    status.update(label="Something went wrong", state="error")
                    st.error("Sorry, something went wrong on our side. Please try again in a moment.")

    res = st.session_state.get("result")
    if res is not None:
        render_result(res)


# --------------------------------------------------------------------------- Admin panel
def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def claim_editor(claim: dict, moderator: str, prefix: str) -> None:
    """Review card + override form for one claim."""
    cur_verdict = claim["verdict"]
    st.caption(
        f"ID {claim['id']} · {claim['input_type']} · status **{claim['status']}** · origin {claim['origin']} · "
        f"hits {claim['hits']} · user flags {claim['flags']} · updated {_fmt_ts(claim['updated_at'])}"
    )
    st.text_area("Claim (PII-redacted)", claim.get("sample_text") or claim.get("norm_text") or "",
                 height=110, disabled=True, key=f"{prefix}_txt")
    st.markdown(f"**Current verdict:** {cur_verdict} ({claim['confidence']:.0f}%)"
                + (f" · _AI's withheld leaning: {claim['raw_verdict']}_"
                   if claim.get("raw_verdict") and claim["raw_verdict"] != cur_verdict else ""))
    if claim["sources"]:
        st.markdown("Sources: " + " · ".join(
            f"[{_md_safe(s['title'])}]({s['url']})" for s in claim["sources"][:4]
            if str(s["url"]).lower().startswith(("http://", "https://"))))

    with st.form(f"{prefix}_form"):
        options = list(VERDICTS) + [REVOKE]
        verdict = st.selectbox("Correct verdict", options,
                               index=options.index(cur_verdict) if cur_verdict in options else 2)
        c1, c2 = st.columns(2)
        category = c1.selectbox("Category", CATEGORIES,
                                index=CATEGORIES.index(claim["category"]) if claim["category"] in CATEGORIES
                                else len(CATEGORIES) - 1)
        lang = c2.selectbox("Language of the explanation below", LANG_TAGS,
                            index=LANG_TAGS.index(claim["language_tag"]) if claim["language_tag"] in LANG_TAGS else 0)
        explanation = st.text_area("Neutral explanation (shown to users)", claim["explanation"] or "", height=120)
        action = st.text_input("Recommended action", claim["recommended_action"] or "")
        reason = st.text_input("Reason for change (required, stored in audit trail)")
        if st.form_submit_button("💾 Apply (takes effect immediately)", type="primary"):
            if not reason.strip():
                st.error("Please give a reason.")
            elif verdict == REVOKE:
                engine.db.admin_revoke(claim["id"], moderator, reason)
                engine.invalidate_cache()
                st.success("Entry revoked; it will no longer be served.")
                st.rerun()
            elif not explanation.strip():
                st.error("Explanation cannot be empty.")
            else:
                engine.db.admin_override(
                    claim["id"], verdict=verdict, category=category, explanation=explanation.strip(),
                    recommended_action=action.strip(), language_tag=lang, moderator=moderator, reason=reason)
                engine.invalidate_cache()
                st.success("Saved. Future identical or near-identical forwards now get this verdict instantly.")
                st.rerun()


def admin_tab() -> None:
    if not settings.admin_password:
        st.warning("The moderator panel is disabled. Set `ADMIN_PASSWORD` in your `.env` to enable it.")
        return

    if not st.session_state.get("admin_ok"):
        st.subheader("Moderator sign-in")
        name = st.text_input("Your name (recorded in the audit trail)")
        pw = st.text_input("Password", type="password")
        if st.button("Sign in"):
            try:
                engine.limiter.check(f"admin-login:{client_id()}", 5, 300, "admin login")
            except RateLimitExceeded as e:
                st.error(f"Too many attempts. Try again in {e.retry_after}s.")
                return
            if name.strip() and hmac.compare_digest(pw.encode(), settings.admin_password.encode()):
                st.session_state.admin_ok, st.session_state.moderator = True, name.strip()
                engine.db.log_admin_action(name.strip(), None, "login", "")
                st.rerun()
            else:
                st.error("Invalid name or password.")
        return

    moderator = st.session_state.moderator
    top = st.columns([6, 1])
    top[0].caption(f"Signed in as **{moderator}**")
    if top[1].button("Sign out"):
        st.session_state.admin_ok = False
        st.rerun()

    t_queue, t_all, t_add, t_ops = st.tabs(["📥 Review queue", "🗂️ All entries", "➕ Add / blacklist", "📊 Ops & audit"])

    with t_queue:
        queue = engine.db.review_queue(50)
        st.caption("Items the AI declined to judge, plus anything users flagged — most-forwarded first.")
        if not queue:
            st.success("Queue is empty 🎉")
        for c in queue:
            title = truncate((c.get("sample_text") or "").replace("\n", " "), 90) or f"Claim {c['id']}"
            with st.expander(f"🔥 {c['hits']} hits · 🚩 {c['flags']} · {title}"):
                claim_editor(c, moderator, f"q{c['id']}")

    with t_all:
        c1, c2 = st.columns([3, 1])
        q = c1.text_input("Search text")
        status = c2.selectbox("Status", ["(any)", STATUS_ADMIN, STATUS_AI, STATUS_PENDING, STATUS_REVOKED])
        for c in engine.db.search_claims(q, None if status == "(any)" else status, 50):
            title = truncate((c.get("sample_text") or c.get("norm_text") or "").replace("\n", " "), 80)
            with st.expander(f"[{c['verdict']}] {title}"):
                claim_editor(c, moderator, f"a{c['id']}")

    with t_add:
        st.caption("Add a known rumour (or a confirmed-true claim) directly to the knowledge base / blacklist.")
        with st.form("add_form", clear_on_submit=True):
            txt = st.text_area("Rumour text exactly as it circulates", height=130)
            verdict = st.selectbox("Verdict", list(VERDICTS), index=1)
            c1, c2 = st.columns(2)
            category = c1.selectbox("Category", CATEGORIES, index=len(CATEGORIES) - 1)
            lang = c2.selectbox("Explanation language", LANG_TAGS)
            expl = st.text_area("Neutral explanation", height=100)
            action = st.text_input("Recommended action")
            src = st.text_input("Official source URL (optional)")
            if st.form_submit_button("Add to database", type="primary"):
                pre = preprocess(txt, settings.max_input_chars)
                if pre.is_empty or not expl.strip():
                    st.error("Text and explanation are required.")
                else:
                    sources = ([{"title": src, "url": src, "domain": "", "trust_tier": "official"}]
                               if src.lower().startswith(("http://", "https://")) else [])
                    cid = engine.db.save_claim(
                        input_type="text", verdict=verdict, confidence=99.0, category=category,
                        recommended_action=action, explanation=expl.strip(), evidence=[], sources=sources,
                        language_tag=lang, status=STATUS_ADMIN, origin="admin",
                        sample_text=redact_pii(truncate(pre.cleaned, 1000)), text_hash=pre.text_hash,
                        norm_text=pre.normalized, tokens=pre.tokens)
                    engine.db.log_admin_action(moderator, cid, "add", "manual entry", verdict)
                    engine.invalidate_cache()
                    st.success(f"Added as claim #{cid}.")

    with t_ops:
        s = engine.db.stats()
        m = st.columns(4)
        m[0].metric("Requests (24h)", s["requests_24h"])
        m[1].metric("AI calls (24h)", s["ai_calls_24h"])
        m[2].metric("Zero-token answers (24h)", s["zero_token_hits_24h"])
        m[3].metric("Tokens (24h)", f"{s['tokens_24h']:,}")
        st.write("Claims by status:", s["claims_by_status"])
        st.write(f"Memory cache entries: {len(engine.cache)} · AI engine: "
                 f"{'✅ configured' if engine.llm.enabled else '❌ not configured'} (model: {engine.llm.model})")
        st.markdown("**Recent requests (PII-redacted)**")
        rows = engine.db.recent_audit(100)
        for r in rows:
            r["ts"] = _fmt_ts(r["ts"])
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.markdown("**Moderator actions**")
        acts = engine.db.recent_admin_actions(50)
        for r in acts:
            r["ts"] = _fmt_ts(r["ts"])
        st.dataframe(acts, use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- layout
st.title("🛡️ HLRN — Hybrid Local Rumor Neutralizer")
st.caption("Check a forwarded message before you share it. We compare it with verified records and live "
           "official sources, and say so honestly when we are not sure.")

with st.sidebar:
    st.markdown("### How it works")
    st.markdown("1. ⚡ **Instant match** with already-verified rumours\n"
                "2. 🔎 **Live search** of official sources & fact-checkers\n"
                "3. 🛡️ **Safety rules**: if we're not sure, a human reviews it")
    if not engine.llm.enabled:
        st.warning("AI engine not configured — only the verified database is active.")

tab_verify, tab_admin = st.tabs(["🔍 Verify", "🧑‍⚖️ Moderator Panel"])
with tab_verify:
    verify_tab()
with tab_admin:
    admin_tab()
