"""
Smart Form Filler — two-phase pipeline

Phase 1 — CLASSIFY: What type is this field?
  ├── Check FieldSemanticsDB (global, cross-domain, instant)
  ├── Static keyword rules (no AI, ~40 patterns)
  └── AI classifier (batch, saves result globally for next time)

Phase 2 — ANSWER: What value goes in the field?
  ├── personal.* / social.* / work_auth.* / compliance.* → profile data (instant)
  ├── file.* → sentinel for extension to show upload UI
  └── question.* → check answer cache → AI generate if miss → cache it

The more forms you fill, the fewer AI tokens are spent.
After a few applications, most sites are fully instant.
"""
import json
import re
from typing import Dict, List, Optional, Tuple

import anthropic

from job_agent.db.field_semantics import FieldSemanticsDB, CANONICAL_TYPES, normalize_label
from job_agent.models import UserProfile
from job_agent.config import AIConfig


# ── Static keyword → canonical_type rules ──────────────────────────────────────
# Checked BEFORE the AI. Covers the most common ATS field names.
# Pattern match: if any keyword in the list appears in the normalized label/name → type

_KEYWORD_RULES: List[Tuple[List[str], str]] = [
    # Personal
    (["first name", "fname", "given name", "firstname", "first-name"], "personal.first_name"),
    (["last name", "lname", "surname", "family name", "lastname", "last-name"], "personal.last_name"),
    (["full name", "fullname", "legal name", "your name", "candidate name", "name"], "personal.full_name"),
    (["email", "e-mail", "email address", "work email", "personal email"], "personal.email"),
    (["phone", "mobile", "cell", "telephone", "phone number", "contact number"], "personal.phone"),
    (["city", "town", "municipality"], "personal.city"),
    (["state", "province", "region"], "personal.state"),
    (["zip", "postal", "postcode", "zip code", "postal code"], "personal.zip"),
    (["country", "nation", "country of residence"], "personal.country"),
    (["address line 1", "address1", "addr1", "street address", "mailing address",
      "home address", "address line"], "personal.address"),
    (["address line 2", "address2", "addr2", "apt", "suite", "unit"], "personal.address2"),
    # Social
    (["linkedin", "linkedin url", "linkedin profile", "linked in"], "social.linkedin"),
    (["github", "git hub", "github url", "github profile"], "social.github"),
    (["portfolio", "website", "personal site", "personal website", "web site"], "social.portfolio"),
    (["twitter", "x handle", "twitter handle"], "social.twitter"),
    # Work auth
    (["authorized to work", "work authorization", "legally authorized",
      "eligible to work", "right to work"], "work_auth.authorized"),
    (["sponsorship", "visa sponsorship", "require sponsorship",
      "need sponsorship", "require work authorization", "visa"], "work_auth.sponsorship"),
    (["willing to relocate", "open to relocation", "relocate",
      "relocation", "would you relocate"], "work_auth.relocate"),
    (["remote", "open to remote", "remote work", "work from home",
      "hybrid", "on-site preference"], "work_auth.remote"),
    # Compliance
    (["background check", "background screening", "consent to background"], "compliance.background"),
    (["drug test", "drug screening", "drug free"], "compliance.drug_test"),
    (["18 years", "over 18", "age 18", "18 or older", "at least 18"], "compliance.over_18"),
    (["felony", "criminal conviction", "criminal history"], "compliance.felony"),
    # Compensation
    (["desired salary", "expected salary", "salary expectation",
      "compensation expectation", "target salary"], "compensation.desired"),
    (["minimum salary", "minimum acceptable", "salary floor"], "compensation.minimum"),
    (["salary", "compensation", "pay"], "compensation.desired"),  # fallback
    # Experience
    (["years of experience", "how many years", "years experience",
      "total experience", "work experience years"], "experience.years_total"),
    (["years in", "experience in this field", "years in role",
      "years in this"], "experience.years_role"),
    (["degree", "highest degree", "education level",
      "highest level of education", "highest education"], "experience.degree"),
    (["major", "field of study", "area of study", "concentration",
      "study program"], "experience.field"),
    (["gpa", "grade point average", "academic gpa"], "experience.gpa"),
    (["graduation year", "year graduated", "grad year"], "experience.grad_year"),
    # Files
    (["upload resume", "attach resume", "resume upload", "cv upload",
      "upload cv", "resume file", "upload your resume"], "file.resume"),
    (["cover letter", "cover letter upload", "attach cover letter",
      "upload cover letter"], "file.cover_letter_doc"),
    # Open-ended questions
    (["why do you want to work", "why are you interested in",
      "why this company", "why us", "why join", "interest in this role",
      "what draws you", "what attracted you"], "question.why_company"),
    (["cover letter", "additional information", "anything else",
      "additional comments", "additional context", "tell us more",
      "supporting statement"], "question.cover_letter"),
    (["tell us about yourself", "tell me about yourself",
      "introduce yourself", "brief introduction",
      "about you", "about yourself"], "question.tell_me_about"),
    (["greatest strength", "biggest strength", "key strength",
      "top strength", "professional strength", "primary strength"], "question.greatest_strength"),
    (["weakness", "area for improvement", "area of growth",
      "development area", "greatest weakness"], "question.greatest_weakness"),
    (["career goals", "where do you see yourself", "5 year",
      "five year", "long-term goals", "career aspirations",
      "professional goals"], "question.career_goals"),
    (["accomplishment", "achievement", "proud of", "biggest win",
      "most proud", "proudest moment", "greatest achievement"], "question.accomplishment"),
    (["challenge", "obstacle", "difficult situation", "overcame",
      "hardest problem", "toughest challenge"], "question.challenge"),
    (["work style", "how do you work", "work best",
      "working style", "preferred work environment"], "question.work_style"),
    (["leadership", "leader", "managed a team", "led a team",
      "leadership experience", "leadership style"], "question.leadership"),
    (["teamwork", "collaboration", "team player", "worked with team",
      "cross-functional"], "question.teamwork"),
    (["why leaving", "why are you leaving", "why did you leave",
      "reason for leaving", "what is motivating"], "question.why_leaving"),
    (["start date", "earliest start", "available to start",
      "when can you start", "availability"], "question.availability"),
    (["how did you hear", "referral source", "how did you find",
      "how did you learn about", "source of application"], "question.referral"),
]


# ── AI prompts ─────────────────────────────────────────────────────────────────

_CLASSIFIER_SYSTEM = """You are a job application form field classifier.
Given form field descriptors (label, name attribute, type, placeholder), classify each into
the most accurate canonical type from the provided list.

Return ONLY valid JSON — no markdown, no explanation:
[
  {"field_id": "...", "canonical_type": "personal.first_name", "confidence": 0.97}
]

Rules:
- Pick the MOST SPECIFIC type that fits
- Use "question.other" only when nothing else fits
- Use "unknown" only for non-application fields (search bars, nav elements, etc.)
- Confidence: 1.0 = certain, 0.7 = probable, below 0.6 = uncertain"""


_ANSWER_SYSTEM = """You are a professional job application writer.
Generate compelling, genuine answers for open-ended job application questions.

Critical rules:
- Use ONLY information from the provided profile — never invent experiences
- Sound human and specific — avoid generic corporate speak and clichés
- Tailor to the specific company and role when provided
- Keep answers concise (3-5 sentences) unless the question type calls for more
- For "why this company" — reference something real and specific about them if possible
  from the job context; if not, focus on what the role enables for the candidate
- For strength/weakness — ground in a real example from their experience
- For career goals — connect current trajectory to where the role leads
- Do not use bullet points — write flowing prose
- End with a forward-looking statement that connects to this opportunity

Return ONLY valid JSON — no markdown:
[
  {"canonical_type": "question.why_company", "answer": "..."}
]"""


class SmartFormFiller:
    """
    Replaces FormFiller. Uses FieldSemanticsDB for global cross-domain learning
    and a separate AI answer cache for open-ended questions.
    """

    def __init__(self, config: AIConfig, semantics: FieldSemanticsDB):
        self.client    = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model     = config.scoring_model
        self.semantics = semantics

    # ── Main entry point ───────────────────────────────────────────────────────

    def fill_fields(
        self,
        fields: List[dict],
        profile: UserProfile,
        job_context: Optional[dict] = None,
        domain: str = "",
    ) -> Tuple[List[dict], dict]:
        """
        fields: [{id, label, name, type, placeholder, options, required, current_value}]
        Returns: (fills, meta)
          fills: [{field_id, value, canonical_type, confidence, source, fingerprint}]
          meta:  {instant_hits, ai_classifier_calls, ai_answer_calls, answer_cache_hits}
        """
        profile_data = self._profile_to_dict(profile, job_context)
        company = (job_context or {}).get("company", "")
        job_title = (job_context or {}).get("title", "")

        meta = {"instant_hits": 0, "ai_classifier_calls": 0,
                "ai_answer_calls": 0, "answer_cache_hits": 0}

        # ── Phase 1: Classify all fields ──────────────────────────────────────
        classified = self._classify_all(fields, domain, meta)

        # ── Phase 2: Generate answers for question types ───────────────────────
        question_types = list({
            c["canonical_type"]
            for c in classified.values()
            if c["canonical_type"].startswith("question.")
        })
        answers = self._get_question_answers(question_types, profile, job_context, meta)

        # ── Phase 3: Resolve values ────────────────────────────────────────────
        fills = []
        for field in fields:
            fid = field["id"]
            cls = classified.get(fid, {"canonical_type": "unknown", "confidence": 0, "source": "unknown"})
            canonical = cls["canonical_type"]
            fp = FieldSemanticsDB.fingerprint(
                field.get("label", ""), field.get("name", ""), field.get("type", "text")
            )

            value = self._resolve_value(
                canonical, profile_data, answers,
                field.get("options", []), field.get("type", "text")
            )

            fills.append({
                "field_id":      fid,
                "value":         value,
                "canonical_type": canonical,
                "human_label":   CANONICAL_TYPES.get(canonical, canonical),
                "profile_key":   canonical,
                "confidence":    cls["confidence"],
                "source":        cls["source"],
                "fingerprint":   fp,
                "is_question":   canonical.startswith("question."),
            })

        return fills, meta

    # ── Phase 1: Classification ────────────────────────────────────────────────

    def _classify_all(self, fields: List[dict], domain: str, meta: dict) -> dict:
        """Returns {field_id: {canonical_type, confidence, source}}"""
        result = {}
        needs_ai = []

        for field in fields:
            fid  = field["id"]
            fp   = FieldSemanticsDB.fingerprint(
                field.get("label", ""), field.get("name", ""), field.get("type", "text")
            )
            label = field.get("label", "")
            name  = field.get("name", "")
            ftype = field.get("type", "text")

            # 1. Global semantics DB (instant, cross-domain)
            known = self.semantics.get_semantic(fp)
            if known:
                result[fid] = {"canonical_type": known, "confidence": 0.97, "source": "learned"}
                meta["instant_hits"] += 1
                continue

            # 2. Static keyword rules (fast, no API)
            static = self._static_classify(label, name, ftype)
            if static:
                result[fid] = {"canonical_type": static, "confidence": 0.93, "source": "static"}
                # Save to global DB so next time it's "learned"
                self.semantics.record_semantic(fp, static, label, name, domain)
                meta["instant_hits"] += 1
                continue

            # 3. File upload shortcut
            if ftype == "file":
                ctype = "file.resume" if any(
                    w in label.lower() + name.lower()
                    for w in ["resume", "cv", "curriculum"]
                ) else "file.other"
                result[fid] = {"canonical_type": ctype, "confidence": 0.9, "source": "static"}
                self.semantics.record_semantic(fp, ctype, label, name, domain)
                meta["instant_hits"] += 1
                continue

            needs_ai.append(field)

        # 4. AI classifier for anything not resolved above
        if needs_ai:
            ai_results = self._ai_classify(needs_ai)
            meta["ai_classifier_calls"] = 1
            for field, cls in zip(needs_ai, ai_results):
                fid  = field["id"]
                fp   = FieldSemanticsDB.fingerprint(
                    field.get("label", ""), field.get("name", ""), field.get("type", "text")
                )
                result[fid] = cls
                # Only save to DB if AI was confident
                if cls["confidence"] >= 0.75 and cls["canonical_type"] != "unknown":
                    self.semantics.record_semantic(
                        fp, cls["canonical_type"],
                        field.get("label", ""), field.get("name", ""), domain
                    )

        return result

    def _static_classify(self, label: str, name: str, ftype: str) -> Optional[str]:
        norm_label = normalize_label(label)
        norm_name  = re.sub(r'[^a-z0-9\s]', ' ', name.lower())

        for keywords, canonical_type in _KEYWORD_RULES:
            for kw in keywords:
                if kw in norm_label or kw in norm_name:
                    # Extra guard: "name" alone is ambiguous — only match if not clearly something else
                    if kw == "name":
                        if any(w in norm_label + norm_name for w in
                               ["company", "employer", "org", "school", "university",
                                "first", "last", "middle", "title", "job"]):
                            continue
                    return canonical_type
        return None

    def _ai_classify(self, fields: List[dict]) -> List[dict]:
        """Batch-classify unknown fields with Claude."""
        types_list = "\n".join(f"  {k}: {v}" for k, v in CANONICAL_TYPES.items())
        simplified = [
            {"field_id": f["id"], "label": f.get("label",""),
             "name": f.get("name",""), "type": f.get("type","text"),
             "placeholder": f.get("placeholder",""),
             "options_sample": f.get("options",[][:5])}
            for f in fields
        ]
        prompt = f"""Available canonical types:
{types_list}

Fields to classify:
{json.dumps(simplified, indent=2)}"""

        raw = ""
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_CLASSIFIER_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            m = re.search(r'\[[\s\S]*\]', raw)
            parsed = json.loads(m.group(0) if m else raw)
            by_id = {item["field_id"]: item for item in parsed}
            return [
                {
                    "canonical_type": by_id.get(f["id"], {}).get("canonical_type", "unknown"),
                    "confidence":     by_id.get(f["id"], {}).get("confidence", 0.5),
                    "source": "ai",
                }
                for f in fields
            ]
        except Exception as e:
            import warnings
            warnings.warn(
                f"[form_filler] AI classify failed ({type(e).__name__}: {e}). "
                f"Raw response: {raw[:300]!r}",
                stacklevel=2,
            )
            return [{"canonical_type": "unknown", "confidence": 0, "source": "ai"} for _ in fields]

    # ── Phase 2: Question Answers ──────────────────────────────────────────────

    def _get_question_answers(
        self,
        question_types: List[str],
        profile: UserProfile,
        job_context: Optional[dict],
        meta: dict,
    ) -> Dict[str, str]:
        """Returns {canonical_type: answer_text}"""
        if not question_types:
            return {}

        company   = (job_context or {}).get("company", "")
        job_title = (job_context or {}).get("title", "")
        answers = {}
        needs_ai = []

        for qtype in question_types:
            cached = self.semantics.get_cached_answer(qtype, company)
            if cached:
                answers[qtype] = cached
                meta["answer_cache_hits"] += 1
            else:
                needs_ai.append(qtype)

        if needs_ai:
            ai_answers = self._ai_generate_answers(needs_ai, profile, job_context)
            meta["ai_answer_calls"] = 1
            for qtype, answer in ai_answers.items():
                answers[qtype] = answer
                self.semantics.cache_answer(qtype, company, job_title, answer)

        return answers

    def _ai_generate_answers(
        self,
        question_types: List[str],
        profile: UserProfile,
        job_context: Optional[dict],
    ) -> Dict[str, str]:
        """Use Claude to write professional answers for open-ended question fields."""
        company   = (job_context or {}).get("company", "")
        job_title = (job_context or {}).get("title", "")

        # Use tailored content if available — this is job-specific and produces better answers
        tailored_summary = (job_context or {}).get("tailored_summary") or profile.summary or ""
        highlighted_skills = (job_context or {}).get("highlighted_skills") or profile.skills or []
        keywords_matched = (job_context or {}).get("keywords_matched") or []

        # Build rich profile context for the AI
        exp_summary = []
        for e in (profile.experience or [])[:4]:
            bullets = "; ".join(e.achievements[:2]) if e.achievements else e.description or ""
            exp_summary.append(
                f"  - {e.title} at {e.company} ({e.start_date or '?'}–{e.end_date or 'Present'}): {bullets}"
            )

        gems = ""
        if getattr(profile, "vault_gems", []):
            gems = "\nHidden strengths from vault:\n" + "\n".join(
                f"  - {g.get('title','')}: {g.get('insight','')}"
                for g in profile.vault_gems[:4]
            )

        kw_line = f"\nKeywords matched for this role: {', '.join(keywords_matched[:10])}" if keywords_matched else ""

        profile_ctx = f"""Candidate: {profile.name}
Current role: {profile.experience[0].title if profile.experience else 'N/A'} at {profile.experience[0].company if profile.experience else 'N/A'}
Summary (tailored for this specific role): {tailored_summary}
Top skills for this role: {', '.join(highlighted_skills[:12])}{kw_line}
Experience:
{chr(10).join(exp_summary)}
{gems}
Unique value propositions: {'; '.join((profile.unique_value_props or [])[:3])}"""

        q_descriptions = [
            {"canonical_type": qt, "question": CANONICAL_TYPES.get(qt, qt)}
            for qt in question_types
        ]

        prompt = f"""Job: {job_title or 'Not specified'} at {company or 'Not specified'}

{profile_ctx}

Generate answers for these question types:
{json.dumps(q_descriptions, indent=2)}

Each answer should be 3-5 sentences, professional, genuine, and specific to this candidate's background.
For why_company answers — focus on what THIS role enables (don't fabricate company knowledge).
For strength answers — use a specific example from their experience.
Return only the JSON array."""

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=_ANSWER_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            m = re.search(r'\[[\s\S]*\]', raw)
            parsed = json.loads(m.group(0) if m else raw)
            return {item["canonical_type"]: item["answer"] for item in parsed}
        except Exception as e:
            print(f"[form_filler] AI answer generation failed: {e}")
            return {qt: "" for qt in question_types}

    # ── Phase 3: Value Resolution ──────────────────────────────────────────────

    def _resolve_value(
        self,
        canonical_type: str,
        profile: dict,
        answers: Dict[str, str],
        options: List[str],
        field_type: str,
    ) -> str:
        """Map a canonical type + profile → concrete string value."""
        cat, _, specific = canonical_type.partition(".")

        if cat == "personal":
            return profile.get(f"personal_{specific}", "")

        if cat == "social":
            return profile.get(f"social_{specific}", "")

        if cat == "work_auth":
            return {"authorized": "Yes", "sponsorship": "No",
                    "relocate": "Yes", "remote": "Yes"}.get(specific, "Yes")

        if cat == "compliance":
            return {"felony": "No"}.get(specific, "Yes")

        if cat == "compensation":
            if specific in ("desired", "minimum"):
                sal = profile.get("salary", "")
                if sal and options:
                    return self._match_option(sal, options)
                return sal
            return ""

        if cat == "experience":
            mapping = {
                "years_total": profile.get("years_experience", ""),
                "years_role":  profile.get("years_experience", ""),
                "degree":      profile.get("degree", ""),
                "field":       profile.get("field_of_study", ""),
                "gpa":         "",
                "grad_year":   profile.get("grad_year", ""),
            }
            val = mapping.get(specific, "")
            if val and options:
                return self._match_option(str(val), options)
            return str(val)

        if cat == "file":
            return "__resume__" if specific == "resume" else "__file__"

        if cat == "question":
            return answers.get(canonical_type, "")

        return ""

    def _match_option(self, value: str, options: List[str]) -> str:
        """Find the closest option in a select/radio list."""
        if not options:
            return value
        v_lower = value.lower()
        # Exact
        for o in options:
            if o.lower() == v_lower:
                return o
        # Contains
        for o in options:
            if v_lower in o.lower() or o.lower() in v_lower:
                return o
        # Numeric range match (e.g. salary "80000" → "75,000 - 100,000")
        try:
            num = float(re.sub(r'[^\d.]', '', value))
            for o in options:
                nums = re.findall(r'[\d,]+', o)
                if len(nums) >= 2:
                    lo = float(nums[0].replace(',', ''))
                    hi = float(nums[1].replace(',', ''))
                    if lo <= num <= hi:
                        return o
        except (ValueError, IndexError):
            pass
        return value  # return original if no match

    # ── Profile Serialization ──────────────────────────────────────────────────

    def _profile_to_dict(self, profile: UserProfile, job_context: Optional[dict]) -> dict:
        first = profile.name.split()[0] if profile.name else ""
        last  = " ".join(profile.name.split()[1:]) if len((profile.name or "").split()) > 1 else ""
        # Prefer explicit city/state fields; fall back to parsing location string
        city  = getattr(profile, "city", "") or (
            profile.location.split(",")[0].strip() if "," in (profile.location or "") else profile.location
        )
        state = getattr(profile, "state", "") or (
            profile.location.split(",")[-1].strip() if "," in (profile.location or "") else ""
        )

        years = self._estimate_years(profile)
        degree = ""
        field_of_study = ""
        grad_year = ""
        if profile.education:
            ed = profile.education[0]
            degree = ed.degree or ""
            field_of_study = ed.field or ""
            grad_year = str(ed.year) if ed.year else ""

        return {
            "personal_first_name": first,
            "personal_last_name":  last,
            "personal_full_name":  profile.name or "",
            "personal_email":      profile.email or "",
            "personal_phone":      profile.phone or "",
            "personal_city":       city or "",
            "personal_state":      state or "",
            "personal_zip":        getattr(profile, "zip_code", "") or "",
            "personal_country":    getattr(profile, "country", "United States") or "United States",
            "personal_address":    getattr(profile, "address_line1", "") or "",
            "personal_address2":   getattr(profile, "address_line2", "") or "",
            "social_linkedin":     getattr(profile, "linkedin_url", "") or "",
            "social_github":       getattr(profile, "github_url", "") or "",
            "social_portfolio":    getattr(profile, "website", "") or getattr(profile, "linkedin_url", "") or "",
            "social_twitter":      "",
            "salary":              str(getattr(profile, "min_salary", 80000)),
            "years_experience":    str(years),
            "degree":              degree,
            "field_of_study":      field_of_study,
            "grad_year":           grad_year,
            "current_title":       profile.experience[0].title if profile.experience else "",
            "current_company":     profile.experience[0].company if profile.experience else "",
        }

    @staticmethod
    def _estimate_years(profile: UserProfile) -> int:
        if not profile.experience:
            return 0
        from datetime import datetime as dt
        earliest = None
        for e in profile.experience:
            if e.start_date:
                try:
                    yr = int(str(e.start_date)[:4])
                    if not earliest or yr < earliest:
                        earliest = yr
                except (ValueError, TypeError):
                    pass
        return dt.now().year - earliest if earliest else max(len(profile.experience) * 2, 1)
