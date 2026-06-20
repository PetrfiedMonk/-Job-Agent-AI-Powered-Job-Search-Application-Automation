"""
Form Pattern Store
Learns field→value mappings per domain so the extension skips the AI call
after the first successful fill. Gets smarter with every application.
"""
import json
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime


class FormPatternStore:
    def __init__(self, db_path: str = "./output/applications.db"):
        self.db_path = Path(db_path)
        self._init_tables()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS form_patterns (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain      TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    label       TEXT,
                    field_name  TEXT,
                    field_type  TEXT,
                    profile_key TEXT,
                    value_hint  TEXT,
                    successes   INTEGER DEFAULT 1,
                    failures    INTEGER DEFAULT 0,
                    last_used   TEXT,
                    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(domain, fingerprint)
                );

                CREATE INDEX IF NOT EXISTS idx_fp_domain ON form_patterns(domain);

                CREATE TABLE IF NOT EXISTS form_submissions (
                    id              TEXT PRIMARY KEY,
                    domain          TEXT,
                    url             TEXT,
                    fields_filled   INTEGER DEFAULT 0,
                    pattern_hits    INTEGER DEFAULT 0,
                    ai_calls        INTEGER DEFAULT 0,
                    submitted_at    TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

    # ── Fingerprinting ─────────────────────────────────────────────────────────

    @staticmethod
    def fingerprint(label: str, name: str, field_type: str) -> str:
        """Stable hash of (label, name, type) — identifies a field across visits."""
        key = f"{label.lower().strip()}|{name.lower().strip()}|{field_type.lower()}"
        return hashlib.md5(key.encode()).hexdigest()[:12]

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_patterns(self, domain: str) -> Dict[str, dict]:
        """
        Return {fingerprint: pattern} for all known fields on this domain.
        Only returns patterns with more successes than failures.
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM form_patterns
                WHERE domain = ? AND successes > failures
                ORDER BY successes DESC
            """, (domain,)).fetchall()
        return {r["fingerprint"]: dict(r) for r in rows}

    def get_domain_fill_rate(self, domain: str) -> dict:
        """Stats for a domain — how well does it auto-fill?"""
        with self._connect() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(successes) as total_successes,
                       SUM(failures) as total_failures
                FROM form_patterns WHERE domain = ?
            """, (domain,)).fetchone()
        if not row or not row["total"]:
            return {"domain": domain, "known_fields": 0, "fill_rate": 0}
        return {
            "domain": domain,
            "known_fields": row["total"],
            "total_successes": row["total_successes"] or 0,
            "total_failures": row["total_failures"] or 0,
        }

    def list_known_domains(self) -> List[dict]:
        """All domains with learned patterns."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT domain, COUNT(*) as field_count,
                       SUM(successes) as fills, MAX(last_used) as last_used
                FROM form_patterns
                GROUP BY domain
                ORDER BY fills DESC
            """).fetchall()
        return [dict(r) for r in rows]

    # ── Write ──────────────────────────────────────────────────────────────────

    def record_success(self, domain: str, fills: List[dict]):
        """
        fills: [{label, name, type, profile_key, value}]
        Upserts each field pattern and increments success count.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            for f in fills:
                fp = self.fingerprint(
                    f.get("label", ""),
                    f.get("name", ""),
                    f.get("type", "text"),
                )
                conn.execute("""
                    INSERT INTO form_patterns
                        (domain, fingerprint, label, field_name, field_type,
                         profile_key, value_hint, successes, last_used)
                    VALUES (?,?,?,?,?,?,?,1,?)
                    ON CONFLICT(domain, fingerprint) DO UPDATE SET
                        successes = successes + 1,
                        profile_key = excluded.profile_key,
                        value_hint  = excluded.value_hint,
                        last_used   = excluded.last_used
                """, (
                    domain, fp,
                    f.get("label", "")[:200],
                    f.get("name", "")[:100],
                    f.get("type", "text"),
                    f.get("profile_key", ""),
                    f.get("value", "")[:500],
                    now,
                ))

    def record_failure(self, domain: str, fingerprints: List[str]):
        """Increment failure count for fields that were wrong / needed correction."""
        with self._connect() as conn:
            for fp in fingerprints:
                conn.execute("""
                    UPDATE form_patterns SET failures = failures + 1
                    WHERE domain = ? AND fingerprint = ?
                """, (domain, fp))

    def log_submission(self, submission_id: str, domain: str, url: str,
                       fields_filled: int, pattern_hits: int, ai_calls: int):
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO form_submissions
                (id, domain, url, fields_filled, pattern_hits, ai_calls)
                VALUES (?,?,?,?,?,?)
            """, (submission_id, domain, url, fields_filled, pattern_hits, ai_calls))
