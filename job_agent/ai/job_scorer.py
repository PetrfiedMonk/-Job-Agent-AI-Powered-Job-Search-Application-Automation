"""
Job Scorer
Scores job postings against the user's profile using Claude.

Improvements:
  #1 Recency bonus  — fresh postings get +0-15 pts added post-scoring
  #4 JD compression — strips boilerplate before sending to Claude (~60% fewer tokens)
"""
import hashlib
import json
import re
from datetime import datetime
from typing import List, Dict

import anthropic

from job_agent.models import JobPosting, UserProfile
from job_agent.config import AIConfig
from typing import Optional


SCORING_SYSTEM_PROMPT = """You are a recruiter and career coach evaluating job-candidate fit.
Score each job posting against the candidate's profile. Be honest and realistic.

Return ONLY a JSON array (no explanation) with one object per job:
[
  {
    "job_id": "id from input",
    "fit_score": 85,
    "salary_score": 70,
    "combined_score": 80,
    "match_reasons": ["Strong PM background", "Python + AI skills match"],
    "gap_reasons": ["Lacks healthcare industry exp"],
    "recommended_keywords": ["product roadmap", "stakeholder management", "agile"],
    "apply": true
  }
]

fit_score    — 0-100: how well the candidate matches requirements
salary_score — 0-100: likelihood salary meets/exceeds candidate's minimum
combined_score — fit_score * 0.65 + salary_score * 0.35
apply — false only if score < 45 or there is a clear dealbreaker"""


# ── Job description compression (#4) ──────────────────────────────────────────

# Heading words that signal relevant content
_SIGNAL = {
    'requirement', 'responsibility', 'qualification', 'skill', 'what you',
    'you will', 'you have', 'we need', 'must have', 'nice to have',
    'experience', 'about the role', 'role overview', 'what we look', 'you bring',
    'you are', 'ideal candidate', 'basic qualifications', 'preferred qualifications',
}

# Heading words that signal boilerplate to skip
_NOISE = {
    'benefit', 'perk', 'compensation package', 'equal opportunity', 'eeo',
    'diversity', 'about us', 'who we are', 'our mission', 'our story',
    'our values', 'why join us', 'what we offer', 'apply now', 'how to apply',
    'accommodation', 'disability', 'veteran',
}


def compress_description(description: str, max_chars: int = 700) -> str:
    """
    Strip EEO statements, benefits sections, and company history.
    Keep requirements, responsibilities, and qualifications.
    Reduces average 2000-char description to ~600 chars — 60% token savings.
    """
    if not description or len(description) <= max_chars:
        return description

    lines = description.splitlines()
    in_signal = True   # optimistic: start collecting
    kept: List[str] = []
    chars = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        lower = stripped.lower()

        # Detect section headers (short lines, often end with colon or are ALL CAPS)
        is_header = (
            len(stripped) < 80 and
            (stripped.endswith(':') or stripped.isupper() or
             any(n in lower for n in _SIGNAL | _NOISE))
        )

        if is_header:
            in_signal = any(s in lower for s in _SIGNAL) and not any(n in lower for n in _NOISE)
            continue  # Don't include the header line itself

        if not in_signal:
            continue

        # Keep bullet points and concise requirement lines
        is_bullet = stripped[:2] in ('• ', '- ', '* ', '· ', '– ', '→ ') or stripped.startswith('•')
        if is_bullet or len(stripped) < 140:
            kept.append(stripped)
            chars += len(stripped)
            if chars >= max_chars:
                break

    if not kept:
        # Fallback: just take the first max_chars chars
        return description[:max_chars]

    return '\n'.join(kept)[:max_chars]


# ── Recency bonus (#1) ─────────────────────────────────────────────────────────

def recency_bonus(posted_date) -> float:
    """
    +0-15 pts based on how recently the job was posted.
    Jobs in the first 48 h statistically get 3-4x more callbacks.
    Decays to 0 after 7 days so staleness doesn't artificially inflate scores.
    """
    if not posted_date:
        return 0.0
    if isinstance(posted_date, str):
        try:
            posted_date = datetime.fromisoformat(posted_date)
        except (ValueError, TypeError):
            return 0.0
    age_h = (datetime.now() - posted_date).total_seconds() / 3600
    if age_h <= 12:  return 15.0
    if age_h <= 24:  return 12.0
    if age_h <= 48:  return 9.0
    if age_h <= 72:  return 5.0
    if age_h <= 120: return 2.0
    return 0.0


# ── Country filter (#5) ───────────────────────────────────────────────────────

# US state abbreviations for fast recognition of domestic locations
_US_STATES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
    'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
    'VA','WA','WV','WI','WY','DC',
}

# Country name tokens that unambiguously identify non-US/non-remote jobs
_FOREIGN_TOKENS = {
    'united kingdom','uk','england','scotland','wales','ireland',
    'canada','australia','new zealand','germany','france','spain','italy',
    'netherlands','belgium','sweden','norway','denmark','finland','switzerland',
    'austria','portugal','poland','czech','india','singapore','hong kong',
    'japan','china','south korea','brazil','mexico','argentina','colombia',
}

def _location_allowed(location: Optional[str], allowed_countries: List[str]) -> bool:
    """Return True if the job location is consistent with any allowed country."""
    if not location:
        return True  # Unknown location — don't discard

    loc = location.strip().lower()

    # Explicit "Remote" match
    remote_allowed = any(c.lower() in ('remote', 'anywhere') for c in allowed_countries)
    if remote_allowed and 'remote' in loc:
        return True

    # US state abbreviation anywhere in location string → domestic job
    us_allowed = any(c.lower() in ('united states', 'us', 'usa') for c in allowed_countries)
    if us_allowed:
        # "City, ST" or "City, ST, USA" — check last non-country token
        parts = [p.strip().upper() for p in re.split(r'[,\s]+', location)]
        if any(p in _US_STATES for p in parts):
            return True
        # Common US suffixes
        if any(t in loc for t in ('united states', ', usa', ', us', 'u.s.')):
            return True

    # Check if location contains a clearly foreign country token — only drop if not allowed
    for token in _FOREIGN_TOKENS:
        if token in loc:
            # See if this foreign country is in the allowed list
            allowed = any(token in c.lower() or c.lower() in token for c in allowed_countries)
            if not allowed:
                return False

    # Couldn't prove it's foreign → keep it (false negatives safer than over-dropping)
    return True


# ── Scorer ─────────────────────────────────────────────────────────────────────

class JobScorer:
    def __init__(self, config: AIConfig):
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model  = config.scoring_model

    def score_batch(
        self, jobs: List[JobPosting], profile: UserProfile, min_score: float = 50.0,
        allowed_countries: Optional[List[str]] = None,
    ) -> List[JobPosting]:
        """
        Score jobs, apply recency bonus, sort by final score, filter by min_score.
        Claude sees compressed descriptions — roughly 60% fewer tokens per job.
        """
        if not jobs:
            return []

        # Dedup by compressed description hash — multi-location copies of same JD
        # only need one Claude call; scores are copied back to the rest.
        desc_to_source: dict = {}  # dhash → first JobPosting in unique set
        unique_jobs: List[JobPosting] = []
        dups: list = []            # (job, source_job) — score will be cloned

        for job in jobs:
            compressed = compress_description(job.description or '')
            dhash = hashlib.md5(compressed.encode()).hexdigest()
            if dhash in desc_to_source:
                dups.append((job, desc_to_source[dhash]))
            else:
                desc_to_source[dhash] = job
                unique_jobs.append(job)

        if dups:
            print(f"[scorer] Deduped {len(dups)} identical descriptions — "
                  f"scoring {len(unique_jobs)}/{len(jobs)} unique JDs")

        print(f"[scorer] Scoring {len(unique_jobs)} jobs (compressed descriptions)…")

        scored: List[JobPosting] = []
        for i in range(0, len(unique_jobs), 10):
            scored.extend(self._score_batch(unique_jobs[i:i + 10], profile))

        # Copy scores from source to duplicate jobs
        source_scores: dict = {id(s): s for s in scored}
        for job, source in dups:
            if id(source) in source_scores:
                src = source_scores[id(source)]
            else:
                src = source
            job.fit_score         = src.fit_score
            job.salary_score      = src.salary_score
            job.combined_score    = src.combined_score
            job.score_breakdown   = dict(src.score_breakdown)
            job.description_summary = src.description_summary
            scored.append(job)

        # Apply recency bonus and sort by final score
        for job in scored:
            bonus = recency_bonus(job.posted_date)
            job.combined_score = min(100.0, job.combined_score + bonus)
            if bonus > 0:
                job.score_breakdown.setdefault('match_reasons', []).append(
                    f"Posted recently (+{bonus:.0f} pts)"
                )

        qualified = [j for j in scored if j.combined_score >= min_score]
        qualified.sort(key=lambda j: j.combined_score, reverse=True)

        # Apply country filter if configured
        if allowed_countries:
            before = len(qualified)
            qualified = [j for j in qualified if _location_allowed(j.location, allowed_countries)]
            dropped = before - len(qualified)
            if dropped:
                print(f"[scorer] Dropped {dropped} jobs outside allowed countries: {allowed_countries}")

        print(f"[scorer] {len(qualified)}/{len(scored)} jobs qualify (≥{min_score})")
        return qualified

    def _score_batch(self, jobs: List[JobPosting], profile: UserProfile) -> List[JobPosting]:
        jobs_payload = []
        for job in jobs:
            summary = compress_description(job.description)
            job.description_summary = summary   # persist to DB via upsert_job
            jobs_payload.append({
                "job_id":      job.id,
                "title":       job.title,
                "company":     job.company,
                "location":    job.location,
                "salary":      job.salary_display,
                "description": summary,         # ~60% fewer tokens sent to Claude
            })

        profile_summary = (
            f"Candidate: {profile.name}\n"
            f"Target roles: {', '.join(profile.target_roles or ['open'])}\n"
            f"Min salary: ${profile.min_salary:,}\n"
            f"Skills: {', '.join(profile.skills[:40])}\n"
            f"Summary: {profile.summary}\n"
            f"Experience: {len(profile.experience)} roles\n"
            f"Certifications: {', '.join(profile.certifications)}"
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SCORING_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": (
                        f"CANDIDATE PROFILE:\n{profile_summary}\n\n"
                        f"JOBS TO SCORE:\n{json.dumps(jobs_payload, indent=2)}"
                    ),
                }],
            )

            raw = response.content[0].text.strip()
            m = re.search(r'\[[\s\S]*\]', raw)
            scores: List[Dict] = json.loads(m.group(0) if m else raw)
            score_map = {s["job_id"]: s for s in scores}

            for job in jobs:
                s = score_map.get(job.id, {})
                job.fit_score     = float(s.get("fit_score", 60))
                job.salary_score  = float(s.get("salary_score", 60))
                job.combined_score= float(s.get("combined_score", 60))
                job.score_breakdown = {
                    "match_reasons":       s.get("match_reasons", []),
                    "gap_reasons":         s.get("gap_reasons", []),
                    "recommended_keywords":s.get("recommended_keywords", []),
                    "apply":               s.get("apply", True),
                }

        except Exception as e:
            print(f"[scorer] Warning: batch scoring failed: {e}")
            for job in jobs:
                job.fit_score = job.salary_score = job.combined_score = 60.0

        return jobs
