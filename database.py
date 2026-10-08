"""
database.py - Persistence for HLRN (SQLite, WAL mode, safe for multi-threaded Streamlit).

Tables
------
claims              Verified / AI-checked claims. This IS the Layer-1 "knowledge base" and,
                    when status='admin_verified' and verdict is Fake, the blacklist.
claim_tokens        Inverted index (token -> claim) used to fetch fuzzy-match candidates
                    without scanning the whole table.
claim_translations  Cached localisations of a verdict so each (claim, language) costs
                    LLM tokens at most once.
audit_logs          PII-redacted request log (retention-limited).
admin_actions       Immutable trail of every moderator override.

Swap-out note: all SQL is isolated in this file; moving to PostgreSQL later only requires
replacing `_conn()` and a handful of SQL dialect details.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from preprocessing import fuzzy_guards_ok, fuzzy_similarity, index_tokens

STATUS_AI = "ai_confirmed"
STATUS_PENDING = "pending_review"
STATUS_ADMIN = "admin_verified"
STATUS_REVOKED = "revoked"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    input_type       TEXT    NOT NULL DEFAULT 'text',      -- text | image | video
    text_hash        TEXT,
    norm_text        TEXT,
    media_key        TEXT,                                  -- sha256(bytes)+caption hash
    phash            TEXT,                                  -- 256-bit dHash (images only)
    caption_hash     TEXT,
    sample_text      TEXT,                                  -- PII-redacted, for moderators
    verdict          TEXT    NOT NULL,
    raw_verdict      TEXT,                                  -- model's verdict before guardrails
    confidence       REAL    NOT NULL,
    category         TEXT,
    recommended_action TEXT,
    explanation      TEXT,
    evidence_json    TEXT,
    sources_json     TEXT,
    language_tag     TEXT,
    status           TEXT    NOT NULL,
    origin           TEXT    NOT NULL,                      -- ai | admin
    hits             INTEGER NOT NULL DEFAULT 0,
    flags            INTEGER NOT NULL DEFAULT 0,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    expires_at       REAL,
    last_hit_at      REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_text  ON claims(text_hash) WHERE input_type = 'text';
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_media ON claims(media_key) WHERE media_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_claims_status  ON claims(status);
CREATE INDEX IF NOT EXISTS ix_claims_caption ON claims(caption_hash);

CREATE TABLE IF NOT EXISTS claim_tokens (
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    token    TEXT    NOT NULL,
    PRIMARY KEY (claim_id, token)
);
CREATE INDEX IF NOT EXISTS ix_tokens_token ON claim_tokens(token);

CREATE TABLE IF NOT EXISTS claim_translations (
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    language_tag TEXT NOT NULL,
    explanation TEXT,
    recommended_action TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (claim_id, language_tag)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    session_id    TEXT,
    client_hash   TEXT,
    input_type    TEXT,
    input_hash    TEXT,
    redacted_input TEXT,
    source_layer  TEXT,
    served_from   TEXT,
    verdict       TEXT,
    confidence    REAL,
    tokens_used   INTEGER DEFAULT 0,
    latency_ms    INTEGER,
    claim_id      INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_logs(ts);

CREATE TABLE IF NOT EXISTS admin_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    moderator   TEXT NOT NULL,
    claim_id    INTEGER,
    action      TEXT NOT NULL,
    old_verdict TEXT,
    new_verdict TEXT,
    reason      TEXT
);
"""


def hamming_hex(a: str, b: str) -> int:
    """Hamming distance between two equal-length hex strings."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self):
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if r is None:
            return None
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
        d["sources"] = json.loads(d.pop("sources_json") or "[]")
        return d

    @staticmethod
    def _live_sql() -> str:
        return "status != 'revoked' AND (expires_at IS NULL OR expires_at > ?)"

    # ------------------------------------------------------------------ Layer-1 lookups
    def find_exact_text(self, text_hash: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute(
                f"SELECT * FROM claims WHERE input_type='text' AND text_hash=? AND {self._live_sql()}",
                (text_hash, time.time()),
            ).fetchone()
        return self._row(r)

    def find_fuzzy_text(
        self, norm_text: str, tokens: Sequence[str], threshold: float, min_tokens: int
    ) -> Optional[Tuple[Dict[str, Any], float]]:
        """
        Fuzzy lookup in two stages:
          1. SQL: fetch <=25 candidates sharing >=50% of the query's informative tokens.
          2. Python: exact similarity + safety guards (numbers / negations must agree).
        """
        if len(tokens) < min_tokens:
            return None
        idx = index_tokens(tokens)[:40]
        if len(idx) < 2:
            return None
        min_shared = max(2, (len(idx) + 1) // 2)
        ph = ",".join("?" * len(idx))
        with self._conn() as c:
            cand = c.execute(
                f"""SELECT claim_id, COUNT(*) AS n FROM claim_tokens
                    WHERE token IN ({ph}) GROUP BY claim_id
                    HAVING n >= ? ORDER BY n DESC LIMIT 25""",
                (*idx, min_shared),
            ).fetchall()
            if not cand:
                return None
            ids = [r["claim_id"] for r in cand]
            rows = c.execute(
                f"""SELECT * FROM claims WHERE id IN ({",".join("?" * len(ids))})
                    AND input_type='text' AND {self._live_sql()}""",
                (*ids, time.time()),
            ).fetchall()
        best: Optional[Tuple[Dict[str, Any], float]] = None
        for r in rows:
            other_tokens = (r["norm_text"] or "").split()
            if not fuzzy_guards_ok(tokens, other_tokens):
                continue
            sim = fuzzy_similarity(norm_text, r["norm_text"] or "")
            # AI-only verdicts have not been human-vetted: demand a tighter match.
            needed = threshold if r["status"] == STATUS_ADMIN else max(threshold, 0.90)
            if sim >= needed and (best is None or sim > best[1]):
                best = (self._row(r), sim)  # type: ignore[arg-type]
        return best

    def find_media_exact(self, media_key: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute(
                f"SELECT * FROM claims WHERE media_key=? AND {self._live_sql()}",
                (media_key, time.time()),
            ).fetchone()
        return self._row(r)

    def find_media_near(
        self, phash_hex: str, caption_hash: str, max_hamming: int
    ) -> Optional[Tuple[Dict[str, Any], float]]:
        """Perceptual near-duplicate lookup for IMAGES (same caption required)."""
        if max_hamming <= 0:
            return None
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT * FROM claims WHERE input_type='image' AND caption_hash=?
                    AND phash IS NOT NULL AND {self._live_sql()}""",
                (caption_hash, time.time()),
            ).fetchall()
        best: Optional[Tuple[sqlite3.Row, int]] = None
        for r in rows:
            try:
                d = hamming_hex(phash_hex, r["phash"])
            except ValueError:
                continue
            if d <= max_hamming and (best is None or d < best[1]):
                best = (r, d)
        if not best:
            return None
        bits = len(phash_hex) * 4
        return self._row(best[0]), 1.0 - best[1] / bits  # type: ignore[return-value]

    def touch(self, claim_id: Optional[int]) -> None:
        if claim_id is None:
            return
        with self._conn() as c:
            c.execute(
                "UPDATE claims SET hits = hits + 1, last_hit_at = ? WHERE id = ?",
                (time.time(), claim_id),
            )

    # ------------------------------------------------------------------ writes
    def save_claim(
        self,
        *,
        input_type: str,
        verdict: str,
        confidence: float,
        category: str,
        recommended_action: str,
        explanation: str,
        evidence: List[str],
        sources: List[Dict[str, Any]],
        language_tag: str,
        status: str,
        origin: str,
        sample_text: str = "",
        text_hash: Optional[str] = None,
        norm_text: Optional[str] = None,
        tokens: Optional[Iterable[str]] = None,
        media_key: Optional[str] = None,
        phash: Optional[str] = None,
        caption_hash: Optional[str] = None,
        raw_verdict: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
    ) -> int:
        """Insert or refresh a claim. A human-verified record is NEVER overwritten by the AI."""
        now = time.time()
        expires = now + ttl_seconds if ttl_seconds else None
        key_sql, key_val = (
            ("input_type='text' AND text_hash=?", text_hash)
            if input_type == "text"
            else ("media_key=?", media_key)
        )
        with self._tx() as c:
            existing = c.execute(f"SELECT id, origin, status FROM claims WHERE {key_sql}", (key_val,)).fetchone()
            if existing:
                if existing["origin"] == "admin" and existing["status"] != STATUS_REVOKED and origin == "ai":
                    return int(existing["id"])
                c.execute(
                    """UPDATE claims SET verdict=?, raw_verdict=?, confidence=?, category=?,
                       recommended_action=?, explanation=?, evidence_json=?, sources_json=?,
                       language_tag=?, status=?, origin=?, sample_text=COALESCE(NULLIF(?, ''), sample_text),
                       updated_at=?, expires_at=? WHERE id=?""",
                    (verdict, raw_verdict, confidence, category, recommended_action, explanation,
                     json.dumps(evidence, ensure_ascii=False), json.dumps(sources, ensure_ascii=False),
                     language_tag, status, origin, sample_text, now, expires, existing["id"]),
                )
                c.execute("DELETE FROM claim_translations WHERE claim_id=?", (existing["id"],))
                return int(existing["id"])
            cur = c.execute(
                """INSERT INTO claims (input_type, text_hash, norm_text, media_key, phash, caption_hash,
                   sample_text, verdict, raw_verdict, confidence, category, recommended_action,
                   explanation, evidence_json, sources_json, language_tag, status, origin,
                   created_at, updated_at, expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (input_type, text_hash, norm_text, media_key, phash, caption_hash, sample_text,
                 verdict, raw_verdict, confidence, category, recommended_action, explanation,
                 json.dumps(evidence, ensure_ascii=False), json.dumps(sources, ensure_ascii=False),
                 language_tag, status, origin, now, now, expires),
            )
            claim_id = int(cur.lastrowid)
            if tokens is not None and input_type == "text":
                c.executemany(
                    "INSERT OR IGNORE INTO claim_tokens (claim_id, token) VALUES (?, ?)",
                    [(claim_id, t) for t in index_tokens(tokens)],
                )
            return claim_id

    # ------------------------------------------------------------------ admin loop
    def admin_override(
        self,
        claim_id: int,
        *,
        verdict: str,
        category: str,
        explanation: str,
        recommended_action: str,
        language_tag: str,
        moderator: str,
        reason: str,
        confidence: float = 99.0,
    ) -> None:
        """Moderator correction. Takes effect immediately for every future Layer-1 lookup."""
        with self._tx() as c:
            old = c.execute("SELECT verdict FROM claims WHERE id=?", (claim_id,)).fetchone()
            if old is None:
                raise KeyError(f"claim {claim_id} not found")
            c.execute(
                """UPDATE claims SET verdict=?, confidence=?, category=?, explanation=?,
                   recommended_action=?, language_tag=?, status=?, origin='admin', flags=0,
                   expires_at=NULL, updated_at=? WHERE id=?""",
                (verdict, confidence, category, explanation, recommended_action, language_tag,
                 STATUS_ADMIN, time.time(), claim_id),
            )
            c.execute("DELETE FROM claim_translations WHERE claim_id=?", (claim_id,))
            c.execute(
                "INSERT INTO admin_actions (ts, moderator, claim_id, action, old_verdict, new_verdict, reason)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), moderator, claim_id, "override", old["verdict"], verdict, reason),
            )

    def admin_revoke(self, claim_id: int, moderator: str, reason: str) -> None:
        with self._tx() as c:
            old = c.execute("SELECT verdict FROM claims WHERE id=?", (claim_id,)).fetchone()
            if old is None:
                raise KeyError(f"claim {claim_id} not found")
            c.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?",
                      (STATUS_REVOKED, time.time(), claim_id))
            c.execute(
                "INSERT INTO admin_actions (ts, moderator, claim_id, action, old_verdict, new_verdict, reason)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), moderator, claim_id, "revoke", old["verdict"], None, reason),
            )

    def log_admin_action(self, moderator: str, claim_id: Optional[int], action: str, reason: str,
                         new_verdict: Optional[str] = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO admin_actions (ts, moderator, claim_id, action, old_verdict, new_verdict, reason)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), moderator, claim_id, action, None, new_verdict, reason),
            )

    def flag_claim(self, claim_id: int) -> None:
        """User pressed 'this looks wrong' -> pushes the claim up the moderator queue."""
        with self._conn() as c:
            c.execute("UPDATE claims SET flags = flags + 1 WHERE id = ?", (claim_id,))

    # ------------------------------------------------------------------ admin reads
    def get_claim(self, claim_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())

    def review_queue(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Pending AI abstentions + anything users flagged, most 'viral' first."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM claims WHERE status != 'revoked' AND (status = 'pending_review' OR flags > 0)
                   ORDER BY (flags * 5 + hits) DESC, updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def search_claims(self, query: str = "", status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM claims WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if query:
            sql += " AND (sample_text LIKE ? OR norm_text LIKE ?)"
            args += [f"%{query}%", f"%{query.lower()}%"]
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql, args).fetchall()]  # type: ignore[misc]

    def recent_admin_actions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM admin_actions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()]

    # ------------------------------------------------------------------ translations
    def get_translation(self, claim_id: int, language_tag: str) -> Optional[Dict[str, str]]:
        with self._conn() as c:
            r = c.execute(
                "SELECT explanation, recommended_action FROM claim_translations WHERE claim_id=? AND language_tag=?",
                (claim_id, language_tag),
            ).fetchone()
        return dict(r) if r else None

    def put_translation(self, claim_id: int, language_tag: str, explanation: str, action: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO claim_translations VALUES (?,?,?,?,?)",
                (claim_id, language_tag, explanation, action, time.time()),
            )

    # ------------------------------------------------------------------ audit
    def log_audit(self, **kw: Any) -> None:
        """Callers MUST pass already-redacted text in `redacted_input`."""
        cols = ["session_id", "client_hash", "input_type", "input_hash", "redacted_input", "source_layer",
                "served_from", "verdict", "confidence", "tokens_used", "latency_ms", "claim_id", "error"]
        with self._conn() as c:
            c.execute(
                f"INSERT INTO audit_logs (ts, {','.join(cols)}) VALUES (?, {','.join('?' * len(cols))})",
                (time.time(), *[kw.get(k) for k in cols]),
            )

    def purge_old_audit(self, retention_days: int) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM audit_logs WHERE ts < ?", (time.time() - retention_days * 86400,))
            return cur.rowcount

    def recent_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM audit_logs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()]

    def stats(self) -> Dict[str, Any]:
        since = time.time() - 86400
        with self._conn() as c:
            by_status = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM claims GROUP BY status")}
            a = c.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN served_from='ai' THEN 1 ELSE 0 END) AS ai,
                          SUM(CASE WHEN served_from IN ('cache','database') THEN 1 ELSE 0 END) AS free,
                          COALESCE(SUM(tokens_used), 0) AS tokens
                   FROM audit_logs WHERE ts > ?""", (since,)).fetchone()
        return {"claims_by_status": by_status, "requests_24h": a["total"] or 0,
                "ai_calls_24h": a["ai"] or 0, "zero_token_hits_24h": a["free"] or 0,
                "tokens_24h": a["tokens"] or 0}
