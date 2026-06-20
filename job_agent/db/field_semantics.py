"""
Global Field Semantics Store

Learns the meaning of form fields across ALL job sites — not per-domain.
Once the system sees that 'fname', 'applicant[first_name]', 'given-name-field'
all mean personal.first_name, that knowledge applies everywhere forever.

Separate concerns:
  field_semantics   — fingerprint → canonical_type (global, cross-domain)
  answer_cache      — canonical_type + company → AI-generated text answer
  form_submissions  — audit log of what was filled where
"""
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


# ── Canonical Type Registry ────────────────────────────────────────────────────
# All valid canonical types the system can produce.
# Format: category.specific_field

CANONICAL_TYPES: Dict[str, str] = {
    # Personal info
    "personal.first_name":    "First name",
    "personal.last_name":     "Last name",
    "personal.full_name":     "Full name",
    "personal.email":         "Email address",
    "personal.phone":         "Phone number",
    "personal.city":          "City",
    "personal.state":         "State / Province",
    "personal.zip":           "ZIP / Postal code",
    "personal.country":       "Country",
    "personal.address":       "Street address",
    # Social / online presence
    "social.linkedin":        "LinkedIn profile URL",
    "social.github":          "GitHub URL",
    "social.portfolio":       "Portfolio / personal website",
    "social.twitter":         "Twitter / X handle",
    # Work authorization
    "work_auth.authorized":   "Authorized to work in this country?",
    "work_auth.sponsorship":  "Require visa sponsorship?",
    "work_auth.relocate":     "Willing to relocate?",
    "work_auth.remote":       "Open to remote work?",
    # Compliance
    "compliance.background":  "Consent to background check",
    "compliance.drug_test":   "Consent to drug test",
    "compliance.over_18":     "Confirm age 18+",
    "compliance.felony":      "Any felony convictions?",
    # Compensation
    "compensation.desired":   "Desired / expected salary",
    "compensation.minimum":   "Minimum acceptable salary",
    "compensation.type":      "Pay type (hourly/salary)",
    # Experience metrics
    "experience.years_total": "Total years of work experience",
    "experience.years_role":  "Years in this specific role/field",
    "experience.degree":      "Highest education degree",
    "experience.field":       "Field / major of study",
    "experience.gpa":         "GPA",
    "experience.grad_year":   "Graduation year",
    # Files
    "file.resume":            "Resume file upload",
    "file.cover_letter_doc":  "Cover letter file upload",
    "file.other":             "Other document upload",
    # Open-ended questions — AI generates answers for these
    "question.why_company":       "Why do you want to work here?",
    "question.cover_letter":      "Cover letter / additional info",
    "question.tell_me_about":     "Tell us about yourself",
    "question.greatest_strength": "Your greatest professional strength",
    "question.greatest_weakness": "Area for improvement / weakness",
    "question.career_goals":      "Career goals / 5-year plan",
    "question.accomplishment":    "Most proud accomplishment",
    "question.challenge":         "Challenge you overcame",
    "question.work_style":        "How you work best / work style",
    "question.leadership":        "Leadership experience or philosophy",
    "question.teamwork":          "Teamwork / collaboration example",
    "question.why_leaving":       "Why leaving / left current role",
    "question.salary_justify":    "Salary expectation with context",
    "question.availability":      "Start date / availability",
    "question.referral":          "How did you hear about this role?",
    "question.other":             "Other open-ended question",
    # Unknown
    "unknown":                    "Unrecognized field",
}


def normalize_label(text: str) -> str:
    """Aggressive normalization so 'Please enter your First Name *' hashes same as 'First name'."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    noise = {'please', 'enter', 'your', 'type', 'input', 'provide', 'the', 'a', 'an',
             'required', 'optional', 'eg', 'ex', 'example', 'here'}
    tokens = [t for t in text.split() if t not in noise]
    return ' '.join(tokens).strip()


class FieldSemanticsDB:
    """
    Global knowledge base of form field semantics.
    One SQLite file shared across all domains.
    """

    def __init__(self, db_path: str = "./output/applications.db"):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ─────────────────────────────────────────────────────────────────

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS field_semantics (
                    fingerprint      TEXT PRIMARY KEY,
                    canonical_type   TEXT NOT NULL,
                    label_examples   TEXT DEFAULT '[]',
                    name_examples    TEXT DEFAULT '[]',
                    domain_examples  TEXT DEFAULT '[]',
                    successes        INTEGER DEFAULT 0,
                    failures         INTEGER DEFAULT 0,
                    last_seen        TEXT,
                    created_at       TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_fs_type ON field_semantics(canonical_type);

                CREATE TABLE IF NOT EXISTS answer_cache (
                    id              TEXT PRIMARY KEY,
                    canonical_type  TEXT NOT NULL,
                    company         TEXT DEFAULT '',
                    job_title       TEXT DEFAULT '',
                    answer          TEXT NOT NULL,
                    word_count      INTEGER DEFAULT 0,
                    created_at      TEXT,
                    used_count      INTEGER DEFAULT 0
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_ac_type_company
                    ON answer_cache(canonical_type, company);
                CREATE INDEX IF NOT EXISTS idx_ac_type ON answer_cache(canonical_type);

                CREATE TABLE IF NOT EXISTS form_submissions (
                    id              TEXT PRIMARY KEY,
                    domain          TEXT,
                    url             TEXT,
                    fields_filled   INTEGER DEFAULT 0,
                    instant_hits    INTEGER DEFAULT 0,
                    ai_calls        INTEGER DEFAULT 0,
                    submitted_at    TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sub_domain ON form_submissions(domain);
            """)

    # ── Fingerprinting ─────────────────────────────────────────────────────────

    @staticmethod
    def fingerprint(label: str, name: str, field_type: str) -> str:
        """
        Stable cross-domain identity for a form field.
        Normalizes label aggressively so minor wording differences hash the same.
        """
        norm_label = normalize_label(label)
        norm_name  = re.sub(r'[^a-z0-9_]', '', name.lower())
        norm_type  = field_type.lower().strip()
        key = f"{norm_label}|{norm_name}|{norm_type}"
        return hashlib.md5(key.encode()).hexdigest()[:14]

    # ── Field Semantics Read ───────────────────────────────────────────────────

    def get_semantic(self, fingerprint: str) -> Optional[str]:
        """Return the canonical type for a fingerprint if we're confident about it."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT canonical_type, successes, failures FROM field_semantics WHERE fingerprint = ?",
                (fingerprint,)
            ).fetchone()
        if not row:
            return None
        # Only trust if successes outweigh failures
        if row["successes"] > 0 and row["successes"] >= row["failures"]:
            return row["canonical_type"]
        return None

    def record_semantic(self, fingerprint: str, canonical_type: str,
                        label: str = "", name: str = "", domain: str = ""):
        """Record or reinforce a fingerprint → canonical_type mapping."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT label_examples, name_examples, domain_examples FROM field_semantics WHERE fingerprint = ?",
                (fingerprint,)
            ).fetchone()

            if existing:
                labels  = json.loads(existing["label_examples"]  or "[]")
                names   = json.loads(existing["name_examples"]   or "[]")
                domains = json.loads(existing["domain_examples"] or "[]")
                if label  and label  not in labels:  labels.append(label)
                if name   and name   not in names:   names.append(name)
                if domain and domain not in domains: domains.append(domain)
                conn.execute("""
                    UPDATE field_semantics
                    SET canonical_type=?, label_examples=?, name_examples=?,
                        domain_examples=?, successes=successes+1, last_seen=?
                    WHERE fingerprint=?
                """, (
                    canonical_type,
                    json.dumps(labels[:10]),
                    json.dumps(names[:10]),
                    json.dumps(domains[:10]),
                    now, fingerprint,
                ))
            else:
                conn.execute("""
                    INSERT INTO field_semantics
                    (fingerprint, canonical_type, label_examples, name_examples,
                     domain_examples, successes, last_seen, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """, (
                    fingerprint, canonical_type,
                    json.dumps([label]  if label  else []),
                    json.dumps([name]   if name   else []),
                    json.dumps([domain] if domain else []),
                    now, now,
                ))
            conn.commit()

    def record_correction(self, fingerprint: str, correct_type: str,
                          label: str = "", name: str = "", domain: str = ""):
        """User corrected our fill — demote the wrong type, promote the right one."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE field_semantics SET failures=failures+1, last_seen=? WHERE fingerprint=?",
                (now, fingerprint)
            )
            conn.commit()
        # The correct type now gets a success
        self.record_semantic(fingerprint, correct_type, label, name, domain)

    # ── Answer Cache Read/Write ────────────────────────────────────────────────

    def get_cached_answer(self, canonical_type: str, company: str = "") -> Optional[str]:
        """
        Return a cached AI answer for this question type.
        Prefers company-specific, falls back to generic.
        """
        with self._connect() as conn:
            row = conn.execute("""
                SELECT answer, id FROM answer_cache
                WHERE canonical_type = ?
                  AND (company = ? OR company = '')
                ORDER BY CASE WHEN company = ? THEN 0 ELSE 1 END, used_count DESC
                LIMIT 1
            """, (canonical_type, company, company)).fetchone()

        if row:
            # Increment usage counter in background
            with self._connect() as conn:
                conn.execute(
                    "UPDATE answer_cache SET used_count=used_count+1 WHERE id=?",
                    (row["id"],)
                )
                conn.commit()
            return row["answer"]
        return None

    def cache_answer(self, canonical_type: str, company: str,
                     job_title: str, answer: str):
        """Save an AI-generated answer for future reuse."""
        now = datetime.now().isoformat()
        word_count = len(answer.split())
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO answer_cache
                    (id, canonical_type, company, job_title, answer, word_count, created_at, used_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(canonical_type, company) DO UPDATE SET
                    answer=excluded.answer,
                    job_title=excluded.job_title,
                    word_count=excluded.word_count,
                    created_at=excluded.created_at
            """, (str(uuid.uuid4()), canonical_type, company,
                  job_title, answer, word_count, now))
            conn.commit()

    # ── Stats / Admin ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        with self._connect() as conn:
            fields = conn.execute(
                "SELECT COUNT(*) as n, SUM(successes) as s FROM field_semantics"
            ).fetchone()
            answers = conn.execute("SELECT COUNT(*) as n FROM answer_cache").fetchone()
            subs = conn.execute("SELECT COUNT(*) as n FROM form_submissions").fetchone()
        return {
            "known_field_fingerprints": fields["n"] or 0,
            "total_successful_fills":   fields["s"] or 0,
            "cached_answers":           answers["n"] or 0,
            "total_submissions":        subs["n"] or 0,
        }

    def list_known_domains(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT domain,
                       COUNT(*) as submissions,
                       SUM(fields_filled) as total_fills,
                       SUM(instant_hits) as total_instant,
                       MAX(submitted_at) as last_used
                FROM form_submissions
                WHERE domain IS NOT NULL AND domain != ''
                GROUP BY domain
                ORDER BY submissions DESC
                LIMIT 50
            """).fetchall()
        return [dict(r) for r in rows]

    def log_submission(self, submission_id: str, domain: str, url: str,
                       fields_filled: int, instant_hits: int, ai_calls: int):
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO form_submissions
                (id, domain, url, fields_filled, instant_hits, ai_calls, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (submission_id, domain, url, fields_filled, instant_hits, ai_calls, now))
            conn.commit()
