# HLRN — Hackathon Pitch Guide

> **Before you present:** the seeded rumours are *demo content* (see `seed_data.py` header). Skim each one against current PIB Fact Check / ministry pages, and replace the portal home-page links with exact article URLs where you can. Judges notice sloppy "official" claims; they reward honesty about limits.

---

## Part 1 — Five-slide deck

### Slide 1 · The Problem — "One forward travels faster than any fact-check"
- WhatsApp forwards reach millions in hours; fact-checks arrive days later, in English, on websites ordinary users never open.
- Real harm: panic buying, financial scams (fake KYC, fake jobs, APK malware), health misinformation, and mob violence triggered by "child-lifting gang" forwards.
- Why existing tools fail: (1) one-size-fits-all AI chat is slow, costly and hallucinates; (2) most tools ignore Hinglish / code-mixed text; (3) a wrongly-labelled "FAKE" is a legal and trust risk.
- **Say:** "The same rumour is forwarded 10,000 times. Checking it 10,000 times with an LLM is wasteful — and a confident wrong answer is dangerous."
- *Add 1–2 verified statistics with citations here (e.g. from PIB / MeitY / a published study). Don't quote a number you can't source.*

### Slide 2 · Architecture — "Fast first, smart when needed, human always in the loop"
Draw this (or reuse the README diagram):
```
Forwarded text / image / video
   │  rate limit · noise strip · Hinglish-safe normalise · keyframes (OpenCV)
   ▼
[Memory cache] → [Layer 1: verified DB exact → fuzzy]   0 tokens · milliseconds
   │ miss
   ▼
[Layer 2: Gemini + live Google Search grounding]       official sources only count
   ▼
[Guardrails] <75% confidence → "Needs Human Review" · sensitive topics need 90%
   ▼
Verdict in the user's own language/script  ──►  [Moderator panel] ──► override ──► Layer 1 (instantly)
```
Channels: Streamlit dashboard + WhatsApp/Telegram webhook (`bot_webhook.py`) on the same engine.
Callouts: PII redaction before logs · single-flight (1 AI call per viral burst) · budgets & rate limits.

### Slide 3 · Layer 1 vs Layer 2 — "Pay for intelligence once, serve it a million times"
| | Layer 1 (matcher) | Layer 2 (grounded AI) |
|---|---|---|
| Cost | 0 tokens | tokens + search |
| Latency | ~milliseconds (measured **0.3–0.6 ms** lookup in `seed_data.py`'s self-test; page round-trip is higher) | seconds |
| Handles | re-forwards, emoji/“forwarded” noise, spelling variants | new, unseen claims & images/video |
| Trust | moderator-verified answers win | needs ≥75% confidence **and** an official/fact-checker source |

- Viral maths (exact, not an estimate): a rumour forwarded N times costs a naive pipeline **N** LLM calls; HLRN costs **1** (single-flight + cache + DB), then **N − 1** zero-token answers.
- Video: instead of streaming raw video, OpenCV keeps ≤ 8 distinct frames resized to ≤ 768 px — roughly one image-tile each. Compare against native-video token pricing in the current Gemini docs (it is billed per second of footage) and state **your measured** numbers.
- **Show live:** Moderator Panel → *Ops & audit* tab: "Zero-token answers (24h)" vs "AI calls (24h)" vs "Tokens (24h)".

### Slide 4 · Impact — "Trust, not just detection"
- **Citizens** get an answer in their own language (Hindi / Hinglish / English / regional scripts) *before* they forward.
- **Neutral, non-shaming explanations** (truth-first wording) to reduce the backfire effect.
- **Moderators/fact-checkers** see the most-forwarded unresolved claims first; every override instantly protects every future forward.
- **Governments/NGOs:** auditable (every action logged, PII masked), cost-bounded, legally cautious (never a blunt "Fake" when unsure).
- Metrics to promise & measure in a pilot: median time-to-answer, % answered at zero tokens, moderator turnaround, share of "Needs Human Review".

### Slide 5 · Future scope — "From MVP to national infrastructure"
1. Semantic / multilingual Layer 1 (embeddings) so translations and paraphrases of a known rumour also match. *(Today Layer 1 is lexical; say so.)*
2. Audio transcription (ASR) for voice-note and video claims.
3. Reverse-image / video-provenance search; OCR text hash for screenshot forwards.
4. WhatsApp Business / Telegram production roll-out, regional-language expansion, IVR for feature phones.
5. Postgres + Redis + queue workers for multi-instance scale; moderator SSO; PIB/state fact-check feed ingestion into Layer 1.
6. Community-flag and early-warning dashboard (spikes by region/category).

---

## Part 2 — The 2-minute live demo script

### Setup (10 minutes before, not on stage)
1. `./run.sh` (or `run.bat` / `docker compose up --build`). Confirm the sidebar shows no "AI engine not configured" warning (needs `GEMINI_API_KEY`).
2. **Rehearse Step 4 once** with your key (so you know the output), then wipe AI results so it's live on stage: `python seed_data.py --fresh` (restarts with only the 14 seeded entries). Restart the app afterwards so the memory cache is empty.
3. Open two browser tabs: **Verify** and **Moderator Panel** (log in beforehand). Keep a text file with the snippets below ready to copy.
4. Wi-Fi risk? Steps 1–3 and 5 work offline. Only Step 4 needs the internet — see the fallback.

### Script (≈ 2:00)

| Time | You say | You do |
|---|---|---|
| **0:00–0:15** | "Every day, rumours like this one reach thousands of families. Watch what happens when one lands in HLRN." | Switch to **Verify** tab. |
| **0:15–0:40** | "This is a real-style WhatsApp forward — emojis, 'forwarded many times', Hinglish." | **Paste Snippet A** → Verify. Result appears instantly. Point at the caption **“Answered instantly from a human moderator — 0 AI tokens used.”** "Millisecond lookup, no AI cost, answer in the same Hinglish." |
| **0:40–1:00** | "Same engine, different script." | **Paste Snippet B** (Devanagari) → Verify. "Hindi in, Hindi out. Notice the tone: it doesn't blame the sender." Point to the confidence, category, official sources. |
| **1:00–1:35** | "Now a rumour we've never seen — this one needs the AI layer." | **Paste Snippet C** → Verify. While the progress bar runs: "It's searching official sources live — and the UI never freezes." When it lands: show sources tagged *Official/Fact-checker*, confidence, and the guardrail line. "If confidence is under 75%, or no official source backs it, HLRN refuses to say 'Fake' and sends it to a human." |
| **1:35–1:50** | "And humans make it smarter." | Moderator tab → Review queue → open the item → pick the verdict, add a reason → **Apply**. Back to Verify, paste Snippet C again: instant, **0 tokens**. |
| **1:50–2:00** | "10,000 forwards, one AI call. Trust, speed and cost — together." | Show **Ops & audit** tab: zero-token answers vs AI calls. End. |

### Snippets to paste (copy exactly)

**Snippet A — Hinglish (Layer 1, seeded)**
```
*Forwarded many times*
🚨🚨 Sarkar ki taraf se sabhi students ko free laptop mil raha hai. PM Free Laptop Yojana mein abhi register karein is link par aur apna Aadhaar number bharein. 🙏🙏
Please share with everyone
```

**Snippet B — Hindi / Devanagari (Layer 1, seeded)**
```
आगे भेजें 🔥🔥 सरकार सभी मोबाइल उपभोक्ताओं को 3 महीने का मुफ्त रिचार्ज दे रही है। नीचे दिए गए लिंक पर क्लिक करके अभी रजिस्टर करें।
```

**Snippet C — a novel Hinglish claim (Layer 2, live)**
```
Breaking! Kal raat 11 baje ke baad Delhi NCR mein 8 magnitude ka bhukamp aane wala hai. Sab log ghar se bahar khuli jagah par rahein. Isse sabko forward karo!
```
Expected: *Fake / Misleading* with a high-confidence, official-source-backed explanation (earthquakes cannot be predicted to the hour), **or** *Needs Human Review* if the sources/confidence don't clear the bar. **Both outcomes are a win** — say so: "Either the AI proves it, or the guardrail hands it to a human." AI output varies, so rehearse and keep a spare:

**Snippet C-backup — a deliberately unverifiable local claim (usually → Needs Human Review)**
```
Sector 14 market mein kal dopahar se naya tax lagne wala hai, sabhi dukaan wale apne groups mein forward karein.
```

### Optional 20-second "wow" extras (only if time allows)
- **Honesty demo:** paste the *child-lifting gang* forward from the seed list (`seed_data.py`, entry 6, text starts "Savdhan! Aapke ilake mein…"). HLRN returns **Needs Human Review** — "we'd rather say *we can't confirm this* than label something fake without proof."
- **True claim:** paste the e-EPIC entry (entry 12) → **Verified True**. "It's not just a fake-detector."
- **Image/video:** upload a screenshot of any forward → show the "extracting key frames" progress step.

### If something goes wrong
| Problem | Do this |
|---|---|
| No internet / API error on Step 4 | Say: "Layer 2 is unavailable, so HLRN fails *safe*: it returns Needs Human Review instead of guessing." That is a feature. Continue with moderator step using this item. |
| Layer 1 paste doesn't match | Paste the snippet exactly (no edits). Check the Moderator Panel → *All entries* shows the seeded items; if not, run `python seed_data.py`. |
| Snippet C returns something you don't want to explain | Use C-backup, or lean into it: explain why the guardrail did/didn't fire. |

---

## Part 3 — Judge Q&A (honest answers)

**Why not just ask ChatGPT/Gemini directly?** Cost and speed at scale (Layer 1/caching), grounded official sources instead of memory, guardrails against confident wrong answers, a human feedback loop, and an audit trail.

**What stops it hallucinating a "Fake" verdict and defaming someone?** Definitive verdicts need ≥75% confidence (90% for communal/political topics) *and* an official or fact-checker source found through live search; otherwise it says *Needs Human Review*. The prompt also forbids accusing named private individuals.

**Does Layer 1 understand paraphrases or translations?** Not yet — it matches re-forwards of the same text (noise, emoji, spelling variants, fuzzy), and refuses to match if numbers or negations differ. Embedding-based matching is on the roadmap.

**What about videos with only spoken claims?** Not covered in this MVP (no speech-to-text yet) — it's roadmap item #2.

**Is the data private?** PII (phones, Aadhaar, PAN, e-mails, UPI, OTP, heuristic names) is masked before logging; client IPs are stored only as salted hashes; audit logs auto-expire.

**How does it scale?** The engine is stateless apart from SQLite/caches; production swaps in Postgres + Redis and a queue for workers. The Streamlit UI and the bot webhook already share one engine.

**What if the moderators are wrong?** Every override is logged with name, reason and previous verdict, and can be revoked.
