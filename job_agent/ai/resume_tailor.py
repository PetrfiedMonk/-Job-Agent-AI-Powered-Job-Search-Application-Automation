"""
Resume Tailor
The core intelligence engine. For each job, Claude reads the full job description,
your profile + vault content, and generates a perfectly tailored resume that:
- Mirrors the job's language and keywords (ATS optimization)
- Surfaces the most relevant experience and projects
- Rewrites bullets to emphasize impact relevant to THIS role
- Adds transferable skills you might have overlooked
"""
import json
import re
from collections import Counter
from typing import List, Optional
import anthropic

from job_agent.models import (
    JobPosting, UserProfile, TailoredResume, WorkExperience
)
from job_agent.config import AIConfig


COVER_LETTER_PROMPT = """You are a master cover letter writer. You write letters that get replies because they are specific, personal, and built around proof — not vague claims.

A winning cover letter has exactly this structure:
1. HOOK (first sentence): Something specific about THIS company's product, challenge, or mission — not generic praise. Demonstrates you actually researched them. Then state what makes you the precise fit for this problem.
2. PROOF (second paragraph): Your single most relevant achievement for this role, with a concrete metric. Name the exact skill they are hiring for and show you have already done it at scale.
3. FIT (third paragraph): 1-2 more targeted wins that map to the key requirements in the job description. Tight, no fluff.
4. CLOSE: Confident and direct. Name a specific thing from the job description you want to discuss. Not "I look forward to hearing from you."

Hard rules:
- 3-4 short paragraphs. Under 250 words total.
- Sound like a high-performing human, not a template.
- BANNED phrases: "I am writing to apply", "I am passionate about", "results-driven", "I look forward to hearing from you", "Thank you for your consideration", "seeking a challenging role"
- Only use the candidate's real experiences — never fabricate anything
- Mirror the job's exact terminology (if they say "GTM strategy", write "GTM strategy")

Return ONLY the cover letter body text. Start with the hook sentence — no greeting, no "Dear Hiring Manager", no subject line."""


TAILOR_SYSTEM_PROMPT = """You are an elite resume strategist. Every resume you produce wins interviews because you apply three principles without exception.

PRINCIPLE 1 — MIRROR THE JOB LANGUAGE EXACTLY
ATS systems match strings, not synonyms. Copy the exact phrases, tech names, and nouns from the job description.
"Product strategy" and "product roadmap strategy" are different strings in an ATS. Use their words.

PRINCIPLE 2 — BULLETS ARE PROOF OF IMPACT, NOT LISTS OF DUTIES
Never describe what a person's job was. Describe what they achieved.
Required formula: Power verb + specific action + measurable result.
✓ "Reduced deployment time 60% by migrating CI pipeline to GitHub Actions, unblocking 3 engineering teams."
✗ "Responsible for managing the deployment pipeline."
✗ "Helped with CI/CD improvements."
✗ "Worked on reducing deployment time."

If no exact number exists, use a qualifier: "~30% faster", "3x throughput", "sole engineer on...", "first at company to..."

PRINCIPLE 3 — THE SUMMARY IS A SALES PITCH, NOT A BIO
The summary is the only section a skimming recruiter reads. It must:
(a) Name exactly what kind of role this candidate is the ideal fit for (use their job title)
(b) State the candidate's sharpest competitive edge specific to THIS company and THIS role
(c) Include one knockout metric that proves (b)

BANNED OPENERS that signal amateur resumes (never use these):
"Results-driven", "Passionate about", "Experienced professional", "Self-motivated", "Team player", "Seeking a challenging role", "Detail-oriented"

POWER VERB BANK — use these, no weak synonyms:
Led, Drove, Built, Launched, Engineered, Designed, Scaled, Delivered, Negotiated, Grew,
Reduced, Automated, Shipped, Converted, Recruited, Closed, Restructured, Secured, Authored,
Spearheaded, Architected, Overhauled, Accelerated, Pioneered, Expanded, Streamlined

Return ONLY a JSON object:
{
  "tailored_summary": "3 tight sentences. S1: [exact job title from posting] with [candidate's top competitive edge for THIS specific role at THIS company]. S2: [their single strongest metric-backed achievement most relevant to this job]. S3: [what they uniquely bring to this company's specific challenge or product]. No clichés.",
  "experience": [
    {
      "title": "Job Title",
      "company": "Company",
      "start_date": "2020",
      "end_date": "Present",
      "description": "One line: scope/context — team size, budget, product stage, or key ownership area",
      "achievements": [
        "Power verb + action + measurable result that mirrors JD language",
        "Power verb + action + measurable result (use numbers or qualifiers)",
        "Power verb + action + measurable result (surface transferable wins the candidate might undersell)"
      ],
      "skills_used": ["exact keyword from JD", "another JD keyword"]
    }
  ],
  "highlighted_skills": ["skill1", "skill2"],
  "keywords_matched": ["exact phrase from JD woven naturally into the resume"],
  "ats_score_estimate": 85,
  "cover_letter_opening": "One sentence: specific hook naming something real about this company + what uniquely qualifies this candidate for it"
}

NON-NEGOTIABLE RULES:
- Include only the 3-5 most relevant roles. More roles is not better — focus beats volume.
- Every experience entry MUST have 3-5 achievement bullets (hard minimum: 3)
- Every bullet MUST start with a power verb from the bank above
- Every bullet MUST state a result or metric — estimate with a qualifier if the exact number is unknown
- Do NOT invent jobs, schools, degrees, or companies the candidate did not have
- Do NOT fabricate specific metrics — use qualifiers ("~40% faster", "first at company to...") when exact data is unavailable
- Mirror the JOB's exact wording (if they say "north star metric", write "north star metric")
- The tailored_summary MUST be written specifically for this job posting — generic summaries disqualify
"""


class ResumeTailor:
    def __init__(self, config: AIConfig):
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model = config.resume_model

    def tailor(
        self,
        job: JobPosting,
        profile: UserProfile,
        vault_index=None,  # VaultIndex instance, optional
    ) -> TailoredResume:
        """
        Generate a tailored resume for a specific job posting.

        When vault_index is provided, the most relevant vault notes are
        retrieved by keyword/tag scoring (targeted retrieval).
        Falls back to filtering profile.raw_vault_text if no index is available.
        """
        print(f"[tailor] Tailoring resume for: {job.title} @ {job.company}")

        # ATS keyword injection — find high-value JD terms missing from profile.
        # No extra API call: pure text analysis that tells Claude exactly what to inject.
        profile_text = self._build_profile_context(profile)
        missing_keywords = self._missing_ats_keywords(job.description, profile_text)
        keywords = list(dict.fromkeys(
            job.score_breakdown.get("recommended_keywords", []) + missing_keywords
        ))[:15]
        profile_context = profile_text

        # ── Vault context: indexed retrieval beats raw text slicing ──────────
        if vault_index is not None:
            from job_agent.parsers.vault_index import keywords_from_job_description
            job_keywords = keywords_from_job_description(job.title, job.description)
            job_tags = [w.lower() for w in job.title.split() if len(w) > 3]
            vault_context = vault_index.get_relevant_content(
                query_keywords=job_keywords,
                query_tags=job_tags,
                max_files=8,
                max_chars_per_file=1500,
                max_total_chars=5000,
            )
            print(f"[tailor] Vault: {len(vault_context):,} chars "
                  f"(indexed retrieval for '{job.title}')")
        else:
            vault_context = self._filter_vault_text(profile.raw_vault_text or '', job)

        # Split keywords: ones the profile already contains vs ones that need injection
        profile_lower = profile_context.lower()
        already_present = [k for k in keywords if k.lower() in profile_lower]
        must_inject     = [k for k in keywords if k.lower() not in profile_lower]

        inject_block = ""
        if must_inject:
            inject_block = (
                f"\nATS KEYWORDS TO INJECT (appear in JD, NOT in profile — "
                f"work these exact phrases into bullets or skills naturally):\n"
                + "\n".join(f"  • {k}" for k in must_inject[:8])
            )

        prompt = f"""JOB POSTING:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Salary: {job.salary_display}

JOB DESCRIPTION:
{job.description}

KEYWORDS ALREADY IN PROFILE: {', '.join(already_present) if already_present else 'none detected'}
{inject_block}

---

CANDIDATE PROFILE:
{profile_context}

---

VAULT / ADDITIONAL CONTEXT:
{vault_context if vault_context else '(No additional vault context)'}

Generate a tailored resume JSON for this specific job posting.
"""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=TAILOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```")

        data = json.loads(raw)

        # Build TailoredResume
        tailored = TailoredResume(
            job=job,
            profile=profile,
            tailored_summary=data.get("tailored_summary", profile.summary),
            highlighted_skills=data.get("highlighted_skills", profile.skills[:15]),
            keywords_matched=data.get("keywords_matched", []),
            ats_score_estimate=float(data.get("ats_score_estimate", 0)),
        )

        for exp in data.get("experience", []):
            tailored.tailored_experience.append(WorkExperience(
                title=exp.get("title", ""),
                company=exp.get("company", ""),
                start_date=exp.get("start_date"),
                end_date=exp.get("end_date"),
                description=exp.get("description", ""),
                achievements=exp.get("achievements", []),
                skills_used=exp.get("skills_used", []),
            ))

        print(f"[tailor] Done. ATS estimate: {tailored.ats_score_estimate:.0f}% | "
              f"Keywords matched: {len(tailored.keywords_matched)}")
        return tailored

    def _build_profile_context(self, profile: UserProfile) -> str:
        """Format profile as a clear text block for the AI."""
        lines = [
            f"Name: {profile.name}",
            f"Contact: {profile.email} | {profile.phone} | {profile.location}",
            f"LinkedIn: {profile.linkedin_url}",
            "",
            f"SUMMARY:\n{profile.summary}",
            "",
            f"UNIQUE VALUE PROPS:\n" + "\n".join(f"- {v}" for v in profile.unique_value_props),
            "",
            "WORK EXPERIENCE:",
        ]
        for exp in profile.experience:
            lines.append(f"\n{exp.title} @ {exp.company} ({exp.start_date} - {exp.end_date})")
            lines.append(exp.description)
            for achievement in exp.achievements:
                lines.append(f"  • {achievement}")

        lines += [
            "",
            f"SKILLS: {', '.join(profile.skills)}",
            "",
            f"CERTIFICATIONS: {', '.join(profile.certifications)}",
            "",
            "EDUCATION:",
        ]
        for edu in profile.education:
            lines.append(f"  {edu.degree} - {edu.school} ({edu.year or 'N/A'})")

        lines += ["", "PROJECTS:"]
        for proj in profile.projects:
            name = proj.get("name", "")
            desc = proj.get("description", "")
            impact = proj.get("impact", "")
            lines.append(f"  {name}: {desc} | Impact: {impact}")

        return "\n".join(lines)

    def _missing_ats_keywords(self, job_description: str, profile_text: str, top_n: int = 10) -> List[str]:
        """
        Pure text analysis — zero API cost.
        Extracts high-signal terms from the JD that don't appear in the profile,
        so Claude knows exactly what phrases need to be woven into the resume.

        Strategy:
          1. Extract multi-word technical phrases (highest value — exact ATS triggers)
          2. Extract high-frequency single words after stripping noise
          3. Filter out anything already in the profile
          4. Return top_n sorted by frequency/weight
        """
        STOPWORDS = {
            'the','a','an','and','or','but','in','on','at','to','for','of','with',
            'is','are','was','were','be','been','have','has','had','do','does','did',
            'will','would','could','should','may','might','this','that','these','those',
            'we','you','they','our','your','their','its','by','from','into','through',
            'ability','experience','work','team','role','position','company','candidate',
            'looking','strong','required','preferred','opportunity','responsibilities',
            'qualifications','ideal','proven','including','also','well','very','must',
            'including','as','if','not','no','new','all','more','other','some','than',
            'then','when','where','who','which','how','what','any','each','both',
        }

        jd = job_description
        profile_lower = profile_text.lower()

        # Phase 1: multi-word technical phrases  (e.g. "machine learning", "cross-functional")
        phrases: List[str] = []
        # Title-case adjacent words (often proper nouns / tech stacks)
        phrases += re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b', jd)
        # Hyphenated terms
        phrases += re.findall(r'\b([a-zA-Z][a-zA-Z]+-[a-zA-Z][a-zA-Z]+(?:-[a-zA-Z]+)*)\b', jd)
        # Common bi-grams after lowercasing
        words_lower = re.findall(r'\b[a-z][a-z]{2,}\b', jd.lower())
        bigrams = [f"{words_lower[i]} {words_lower[i+1]}"
                   for i in range(len(words_lower)-1)
                   if words_lower[i] not in STOPWORDS and words_lower[i+1] not in STOPWORDS]
        phrase_freq = Counter(p.lower() for p in phrases + bigrams)

        # Phase 2: single high-frequency words
        single_words = [w for w in words_lower if w not in STOPWORDS and len(w) > 3]
        word_freq = Counter(single_words)

        # Merge: phrases weighted 3x over single words
        candidates: List[tuple] = []
        seen: set = set()

        for phrase, count in phrase_freq.most_common(30):
            if phrase in seen or len(phrase) < 4:
                continue
            if phrase not in profile_lower:
                candidates.append((count * 3, phrase))
            seen.add(phrase)

        for word, count in word_freq.most_common(40):
            if word in seen:
                continue
            if count >= 2 and word not in profile_lower:
                candidates.append((count, word))
            seen.add(word)

        candidates.sort(reverse=True)
        return [term for _, term in candidates[:top_n]]

    def _filter_vault_text(self, vault_text: str, job: JobPosting, max_chars: int = 4000) -> str:
        """Return only vault paragraphs most relevant to this job (saves tokens)."""
        if not vault_text or len(vault_text) <= max_chars:
            return vault_text

        title_words = [w.lower() for w in job.title.split() if len(w) > 3]
        desc_words = [w.lower().strip('.,;:()') for w in job.description.split()[:300] if len(w) > 4]
        keywords = set(title_words + desc_words)

        paragraphs = [p.strip() for p in vault_text.split('\n\n') if p.strip()]
        scored = []
        for para in paragraphs:
            para_lower = para.lower()
            score = sum(1 for kw in keywords if kw in para_lower)
            scored.append((score, para))

        scored.sort(reverse=True)
        result, total = [], 0
        for _, para in scored:
            if total + len(para) + 2 > max_chars:
                break
            result.append(para)
            total += len(para) + 2

        return '\n\n'.join(result) if result else vault_text[:max_chars]

    def generate_cover_letter(self, job: JobPosting, profile: UserProfile) -> str:
        """Generate a personalized cover letter for a specific job."""
        print(f"[tailor] Generating cover letter for: {job.title} @ {job.company}")

        prompt = f"""JOB POSTING:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Salary: {job.salary_display}

JOB DESCRIPTION:
{job.description[:3000]}

CANDIDATE:
Name: {profile.name}
Summary: {profile.summary}

KEY EXPERIENCES:
{self._build_cover_letter_context(profile)}

Write a compelling cover letter for this specific role at {job.company}."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=COVER_LETTER_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def _build_cover_letter_context(self, profile: UserProfile) -> str:
        """Build a concise experience block for cover letter generation."""
        lines = []
        for exp in profile.experience[:3]:
            lines.append(f"{exp.title} @ {exp.company} ({exp.start_date}–{exp.end_date})")
            for achievement in exp.achievements[:2]:
                lines.append(f"  • {achievement}")
        lines += [
            f"\nTop Skills: {', '.join(profile.skills[:15])}",
        ]
        for prop in profile.unique_value_props[:3]:
            lines.append(f"• {prop}")
        return "\n".join(lines)
