"""
Vault Job Title Recommender
Analyzes the user's Obsidian vault + profile to surface the highest-paying
job titles they can realistically land — evidence-based, not aspirational.
"""
import json
import re
from typing import Dict, List, Optional

import anthropic

from job_agent.config import AIConfig
from job_agent.models import UserProfile
from job_agent.parsers.vault_index import VaultIndex


RECOMMEND_SYSTEM_PROMPT = """You are a hard-nosed recruiter and compensation analyst. You have zero interest in flattery or career coaching. You look at a candidate's actual evidence — their work history, vault notes, skills, and projects — and answer one question: what roles can this person actually get hired for today, and what do those roles pay?

You are NOT a career coach suggesting what to aspire toward. You are a hiring manager deciding what pile to put the resume in.

"Realistically land" means:
- Their work history or vault notes directly demonstrate the core competencies required — not adjacent, not theoretical
- A recruiter screening resumes would pass them to a hiring manager without hesitation
- No titles that require management tenure they don't have
- No titles requiring domain expertise that appears nowhere in their evidence

Salary ranges are US market (base salary only, 50th–75th percentile for strong candidates in major metro areas or remote). Do not include equity or total comp in the salary_range field.

Return ONLY a JSON array of exactly 8 objects. No markdown fences, no explanation, just the raw JSON array.

Each object MUST have these exact keys:
{
  "title": "Exact job title as it appears on job boards — capitalize properly (e.g. 'Senior Product Manager')",
  "salary_range": "$XXX,000 – $XXX,000",
  "salary_mid": 135000,
  "confidence": "strong",
  "why_qualified": "2-3 sentences citing specific evidence: company names, projects, tools, metrics from their vault/experience that prove they can do this job. No generic statements.",
  "gap_to_close": "The single most important thing they'd need to strengthen to compete at the top of the candidate pool for this role. Write 'None — already competitive' if they're a strong fit.",
  "search_terms": ["Primary Title", "Alt Title 1", "Alt Title 2"],
  "vault_signal": "1-2 specific vault tags or note themes that most strongly evidence this fit (e.g. '#product-roadmap, project: AI Agent build'). Write 'general experience' if no specific vault signal."
}

Confidence values: "strong" (recruiter advances with no hesitation) | "solid" (good fit, likely passes screening) | "possible" (plausible stretch, needs strong interview prep)

Sort by salary_mid descending — highest-paying realistic role first."""


class VaultRecommender:
    def __init__(self, config: AIConfig):
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model = config.resume_model  # Sonnet — Haiku won't reliably output clean JSON

    def recommend(
        self,
        profile: UserProfile,
        vault_index: Optional[VaultIndex] = None,
    ) -> List[Dict]:
        """
        Analyze the vault + profile and return ranked job title recommendations,
        sorted by realistic total compensation (highest first).
        """
        print("[recommender] Analyzing vault for job title recommendations…")

        vault_overview = ""
        work_notes = ""
        if vault_index:
            try:
                vault_overview = vault_index.get_index_overview()
            except Exception:
                vault_overview = "(vault index unavailable)"
            try:
                work_notes = vault_index.get_category_content(
                    ["work", "project", "skill"],
                    max_files=10,
                    max_chars_per_file=900,
                    max_total_chars=4500,
                )
            except Exception:
                work_notes = ""
        else:
            vault_overview = "(No vault index configured)"

        profile_ctx = self._build_profile_context(profile)

        prompt = (
            "CANDIDATE PROFILE:\n"
            f"{profile_ctx}\n\n"
            "---\n\n"
            "VAULT INDEX OVERVIEW (all notes, tags, and summaries):\n"
            f"{vault_overview[:3000]}\n\n"
            "---\n\n"
            "WORK & PROJECT NOTES (full text of top vault notes by category):\n"
            f"{work_notes[:4500] if work_notes else '(none)'}\n\n"
            "---\n\n"
            "Return the 8 highest-paying job titles this candidate can realistically land today.\n"
            "Sort by salary_mid descending. Use titles that appear verbatim on Indeed and LinkedIn."
        )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=3500,
            system=RECOMMEND_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()

        # Extract JSON array robustly — handles preamble text and fenced code blocks
        match = re.search(r'\[[\s\S]*\]', raw, re.DOTALL)
        if not match:
            print(f"[recommender] Raw response (no JSON array found):\n{raw[:500]}")
            raise ValueError("Claude did not return a JSON array — response had no '[...]' block")
        try:
            recommendations = json.loads(match.group(0))
        except json.JSONDecodeError:
            # Last-ditch: try the whole response after stripping fences
            cleaned = re.sub(r'^```[a-z]*\n?', '', raw, flags=re.MULTILINE)
            cleaned = re.sub(r'\n?```', '', cleaned)
            recommendations = json.loads(cleaned)
        print(f"[recommender] Done — {len(recommendations)} recommendations returned.")
        return recommendations

    def _build_profile_context(self, profile: UserProfile) -> str:
        lines = [
            f"Name: {profile.name}",
            f"Location: {profile.location}",
        ]
        if getattr(profile, "min_salary", None):
            lines.append(f"Min salary target: ${profile.min_salary:,}")
        lines += [
            "",
            f"SUMMARY:\n{profile.summary}",
            "",
        ]
        if profile.unique_value_props:
            lines.append("UNIQUE VALUE PROPOSITIONS:")
            for v in profile.unique_value_props[:6]:
                lines.append(f"  - {v}")
            lines.append("")

        lines.append("WORK HISTORY:")
        for exp in (profile.experience or [])[:6]:
            lines.append(f"\n  {exp.title} @ {exp.company} ({exp.start_date} – {exp.end_date})")
            if exp.description:
                lines.append(f"  {exp.description}")
            for ach in (exp.achievements or [])[:3]:
                lines.append(f"    • {ach}")

        lines += [
            "",
            f"SKILLS ({len(profile.skills)} total): {', '.join(profile.skills[:40])}",
        ]
        if profile.certifications:
            lines.append(f"CERTIFICATIONS: {', '.join(profile.certifications)}")

        if profile.education:
            lines.append("EDUCATION:")
            for edu in profile.education:
                lines.append(f"  {edu.degree} — {edu.school} ({edu.year or 'N/A'})")

        if profile.projects:
            lines.append("\nPROJECTS:")
            for proj in (profile.projects or [])[:5]:
                name = proj.get("name", "")
                desc = proj.get("description", "")
                impact = proj.get("impact", "")
                lines.append(f"  {name}: {desc}" + (f" | Impact: {impact}" if impact else ""))

        vault_skills = getattr(profile, "vault_skills", [])
        if vault_skills:
            lines.append(f"\nVAULT-DISCOVERED SKILLS: {', '.join(vault_skills[:25])}")

        vault_gems = getattr(profile, "vault_gems", [])
        if vault_gems:
            lines.append("\nVAULT GEMS (hidden strengths):")
            for gem in vault_gems[:5]:
                text = gem.get("insight") or gem.get("text") or str(gem) if isinstance(gem, dict) else str(gem)
                lines.append(f"  ★ {text}")

        return "\n".join(lines)
