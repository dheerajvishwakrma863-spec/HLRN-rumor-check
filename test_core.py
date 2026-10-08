"""Run with:  pytest -q   (from the project root)"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["HLRN_DB_PATH"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["GEMINI_API_KEY"] = ""          # tests never touch the network

import engine as E                          # noqa: E402
from config import settings                 # noqa: E402
from preprocessing import (                 # noqa: E402
    fuzzy_guards_ok, preprocess, redact_pii,
)


def test_forward_noise_and_emoji_do_not_change_hash():
    a = preprocess("*Forwarded many times*\n🚨🚨 Sarkaar ne ₹1,000 free de rahi hai!!! Isse sabko forward karo 🙏")
    b = preprocess("sarkar ne rs 1000 free de rahi hain")
    assert a.text_hash == b.text_hash


def test_devanagari_is_preserved_and_digits_normalised():
    p = preprocess("क्या सरकार ₹५०० दे रही है")
    assert "सरकार" in p.normalized and "500" in p.normalized
    assert p.language_tag == "devanagari"


def test_language_detection():
    assert preprocess("Kal school band hai kya, sabko bhej do").language_tag == "hinglish"
    assert preprocess("The government announced a new scheme today").language_tag == "english"


def test_fuzzy_guards_block_negation_and_number_changes():
    assert not fuzzy_guards_ok(preprocess("yeh sach hai 500 rs").tokens, preprocess("yeh sach nahi hai 500 rs").tokens)
    assert not fuzzy_guards_ok(preprocess("pay rs 500 now").tokens, preprocess("pay rs 5000 now").tokens)


def test_pii_redaction():
    out = redact_pii("Call Mr. Ramesh Kumar +91 98765 43210 ramesh@mail.com Aadhaar 1234 5678 9012 otp 481516")
    for leak in ("Ramesh", "98765", "mail.com", "5678", "481516"):
        assert leak not in out


def test_ttl_cache_and_rate_limiter():
    c = E.TTLCache(60, 2)
    c.set("a", 1); c.set("b", 2); c.set("c", 3)
    assert c.get("a") is None and c.get("c") == 3
    rl = E.RateLimiter()
    for _ in range(3):
        rl.check("k", 3, 60)
    try:
        rl.check("k", 3, 60)
        assert False, "should have been limited"
    except E.RateLimitExceeded:
        pass


def _draft(v, conf, cat="Other"):
    return E.LLMDraft(verdict=v, confidence_score=conf, matched_category=cat,
                      neutral_explanation="x", recommended_action="y", supporting_evidence=[])


OFFICIAL = [E.OfficialSource(title="PIB", url="https://factcheck.pib.gov.in/x", trust_tier="official")]


def test_low_confidence_never_blunt_fake():
    f, _ = E.apply_guardrails(_draft(E.VERDICT_FAKE, 74), OFFICIAL, settings, "english")
    assert f["verdict"] == E.VERDICT_REVIEW and f["internal_model_verdict"] == E.VERDICT_FAKE


def test_definitive_verdict_requires_trusted_source_and_high_risk_bar():
    f, _ = E.apply_guardrails(_draft(E.VERDICT_FAKE, 95), [], settings, "english")
    assert f["verdict"] == E.VERDICT_REVIEW
    f, _ = E.apply_guardrails(_draft(E.VERDICT_FAKE, 80, "Communal / Religious"), OFFICIAL, settings, "english")
    assert f["verdict"] == E.VERDICT_REVIEW
    f, _ = E.apply_guardrails(_draft(E.VERDICT_FAKE, 92, "Communal / Religious"), OFFICIAL, settings, "english")
    assert f["verdict"] == E.VERDICT_FAKE


def test_admin_override_is_served_instantly_and_beats_ai():
    eng = E.HLRNEngine(settings)
    pre = preprocess("Government is giving free laptops to all students, register now at laptop.gov.in")
    cid = eng.db.save_claim(
        input_type="text", verdict=E.VERDICT_REVIEW, confidence=50, category="Other", recommended_action="",
        explanation="pending", evidence=[], sources=[], language_tag="english", status="pending_review",
        origin="ai", text_hash=pre.text_hash, norm_text=pre.normalized, tokens=pre.tokens, ttl_seconds=3600)
    eng.db.admin_override(cid, verdict=E.VERDICT_FAKE, category="Government Schemes", explanation="Not true.",
                          recommended_action="Ignore", language_tag="english", moderator="t", reason="test")
    eng.invalidate_cache()
    noisy = "*Forwarded many times* 🚨 " + pre.original.upper() + " 🙏 Please share with everyone"
    r = eng.verify(noisy, client_id="x1")
    assert r.verdict == E.VERDICT_FAKE and r.source_layer == "Layer 1" and r.tokens_used == 0 and r.human_verified
    assert eng.verify(noisy, client_id="x1").served_from == "cache"
    # a later AI write must never overwrite the moderator's decision
    eng.db.save_claim(input_type="text", verdict=E.VERDICT_TRUE, confidence=99, category="Other",
                      recommended_action="", explanation="ai", evidence=[], sources=[], language_tag="english",
                      status="ai_confirmed", origin="ai", text_hash=pre.text_hash, norm_text=pre.normalized)
    eng.invalidate_cache()
    assert eng.verify(noisy, client_id="x2").verdict == E.VERDICT_FAKE


def test_negated_variant_is_not_fuzzy_matched():
    eng = E.HLRNEngine(settings)
    pre = preprocess("PM has announced free recharge for everyone for three months click this link now")
    eng.db.save_claim(input_type="text", verdict=E.VERDICT_FAKE, confidence=99, category="Finance / Scams",
                      recommended_action="", explanation="scam", evidence=[], sources=[], language_tag="english",
                      status="admin_verified", origin="admin", text_hash=pre.text_hash, norm_text=pre.normalized,
                      tokens=pre.tokens)
    r = eng.verify("PM has NOT announced free recharge for everyone for three months click this link now",
                   client_id="x3")
    assert r.source_layer == "Layer 2"           # went to AI path (here: unconfigured fallback)


def test_seed_data_matches_noisy_reforwards():
    import seed_data
    eng = E.HLRNEngine(settings)
    assert seed_data.seed(eng.db) == len(seed_data.RUMOURS)
    assert seed_data.verify(eng.db)          # every seeded rumour found again through forwarding noise


# ---------------------------------------------------------------- model selection (no network)
class _Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class _M:
    def __init__(self, name, actions=("generateContent",)):
        self.name, self.supported_actions = name, list(actions)


class _FakeModels:
    def __init__(self, listing, dead):
        self.listing, self.dead, self.calls = listing, dead, []

    def list(self):
        return self.listing

    def generate_content(self, **kw):
        self.calls.append(kw["model"])
        if kw["model"] in self.dead:
            raise _Err(self.dead[kw["model"]][0], self.dead[kw["model"]][1])
        return "OK"


def _verifier(listing, dead, **cfg_over):
    import dataclasses
    cfg = dataclasses.replace(settings, gemini_api_key="k", **cfg_over)
    v = E.GeminiVerifier(cfg)
    v.enabled = True
    fm = _FakeModels(listing, dead)
    v.client = type("C", (), {"models": fm})()
    return v, fm


def test_model_name_cleaning(monkeypatch=None):
    os.environ["GEMINI_MODEL"] = ' "models/gemini-2.0-flash"  # my model'
    import importlib, config
    importlib.reload(config)
    assert config.settings.gemini_model == "gemini-2.0-flash"
    os.environ.pop("GEMINI_MODEL")
    importlib.reload(config)


def test_discovery_skips_live_audio_and_embedding_models():
    listing = [_M("models/gemini-2.5-flash-native-audio-preview"), _M("models/gemini-live-2.5-flash"),
               _M("models/text-embedding-004", ["embedContent"]), _M("models/gemini-2.5-pro"),
               _M("models/gemini-2.0-flash"), _M("models/gemini-2.5-flash-lite")]
    v, _ = _verifier(listing, {})
    assert v.discover_models() == ["gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]


def test_retired_or_wrong_type_model_falls_back_automatically():
    listing = [_M("models/gemini-2.5-flash"), _M("models/gemini-2.0-flash")]
    dead = {"gemini-2.5-flash": (404, "models/gemini-2.5-flash is no longer available to new users")}
    v, fm = _verifier(listing, dead, gemini_model="gemini-2.5-flash")
    assert v._generate(contents="x") == "OK"
    assert v.model == "gemini-2.0-flash" and fm.calls == ["gemini-2.5-flash", "gemini-2.0-flash"]
    # a live/websocket-only model configured by mistake is skipped before any call is made
    v2, fm2 = _verifier([_M("models/gemini-live-2.5-flash"), _M("models/gemini-2.0-flash")], {},
                        gemini_model="gemini-live-2.5-flash")
    v2._generate(contents="x")
    assert fm2.calls == ["gemini-2.0-flash"]


def test_all_models_dead_gives_actionable_error():
    v, _ = _verifier([_M("models/gemini-2.0-flash")], {"gemini-2.0-flash": (404, "model not found")},
                     gemini_model="gemini-2.0-flash")
    try:
        v._generate(contents="x")
        assert False
    except RuntimeError as e:
        assert "model_doctor.py" in str(e)
