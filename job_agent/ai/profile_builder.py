"""
Profile Builder
Uses Claude to synthesize your Obsidian vault + resume into a rich,
structured UserProfile that powers all downstream resume tailoring.
"""
import json
import re
from typing import Dict, Optional
import anthropic

from job_agent.models import UserProfile, WorkExperience, Education
from job_agent.config import AIConfig


PROFILE_SYNTHESIS_PROMPT = """You are a world-class career advocate and talent analyst. Your mission is not just to summarize a resume — it is to excavate and articulate the FULL professional worth of a human being, including all the value they have quietly accumulated and almost certainly underestimate about themselves.

You have two sources:
1. Their RESUME — the official, sanitized, conservative record they show the world
2. Their OBSIDIAN VAULT — a personal knowledge base of notes, projects, ideas, experiments, learnings, and work-in-progress thinking. This is the unfiltered record of how they actually think and work. It contains enormous professional value that they have never thought to put on a resume.

YOUR CORE MISSION: Find what other people miss.

Most people undersell themselves because:
- They forget about side projects that taught them critical skills
- They don't realize that the way they think and document is itself a rare skill
- They dismiss "small" experiments that actually show initiative, curiosity, and technical range
- They omit cross-domain knowledge that makes them uniquely dangerous in the right role
- The things they do naturally and effortlessly are invisible to them — but gold to an employer

WHAT TO LOOK FOR IN THE VAULT:
- Notes about systems, processes, or problems they tried to solve — that's systems thinking
- Documentation they wrote — that's communication and knowledge-sharing ability
- Research or learning notes — that's intellectual curiosity and self-directed growth
- Side projects or experiments — that's initiative and entrepreneurial thinking
- Notes on tools, workflows, or hacks they developed — that's efficiency and technical creativity
- Any domain knowledge they've accumulated outside their job title — that's rare cross-functional value
- How they structure their thinking (linked notes, frameworks, mental models) — that's strategic cognition

THE SUMMARY must read like a great hiring manager making the case for this candidate. It should be specific, proud, and genuine. Not corporate fluff — real advocacy. Make it feel like someone who truly sees this person's full value wrote it.

UNIQUE VALUE PROPS must be genuinely differentiating — things that make this person rare, not generic ("strong communicator"). Think: "The only person in the room who has both built X and deeply studied Y" or "Ran a full product from 0→1 solo, which means they have no blind spots about how teams fail each other."

VAULT GEMS are the crown jewel of this profile. These are specific discoveries you made IN THE VAULT that are NOT on the resume — hidden strengths the person forgot they had or never thought to mention. Be specific: name the note topic, describe what it reveals about them, and explain exactly why an employer should care. These should feel like treasure found.

Return a JSON object with this exact structure:
{
  "name": "Full Name",
  "email": "email@example.com",
  "phone": "555-555-5555",
  "location": "City, State",
  "linkedin_url": "",
  "summary": "3-5 sentence advocacy statement written in third person. Specific, proud, genuine. Name what makes them rare. Reference real things from their history.",
  "unique_value_props": [
    "Rare differentiator 1 — be specific, name the actual capability or experience",
    "Rare differentiator 2",
    "Rare differentiator 3",
    "Rare differentiator 4"
  ],
  "skills": ["skill1", "skill2"],
  "vault_skills": ["skill found only in vault notes, not resume"],
  "certifications": ["cert1"],
  "experience": [
    {
      "title": "Job Title",
      "company": "Company Name",
      "start_date": "2020",
      "end_date": "Present",
      "description": "What they actually did — not the job description, their real contribution",
      "achievements": [
        "Specific achievement with numbers where inferable — what changed because of them?",
        "Another real impact"
      ],
      "skills_used": ["skill1", "skill2"]
    }
  ],
  "education": [
    {
      "degree": "Degree Name",
      "school": "School Name",
      "field": "Field of Study",
      "year": "Year"
    }
  ],
  "projects": [
    {
      "name": "Project Name",
      "description": "What it does and the real challenge it solved",
      "tech_stack": ["tech1"],
      "url": "",
      "impact": "What this proves about who they are professionally"
    }
  ],
  "vault_gems": [
    {
      "title": "Short name for this hidden strength (e.g. 'Systems Design Thinking')",
      "source": "What in the vault revealed this (e.g. 'Notes on building X', 'Documentation of Y process')",
      "insight": "What this reveals about them professionally — be specific and enthusiastic",
      "why_it_matters": "Exactly how an employer benefits from this hidden skill/knowledge"
    }
  ],
  "vault_insights": {
    "topic_name": "Key insight or capability demonstrated in vault notes"
  }
}

RULES:
- Never invent facts. Only surface what is actually in the provided material.
- Do not be modest on their behalf. If it's there, say it clearly and with confidence.
- Every vault_gem must be a real discovery from the vault content, not the resume.
- If the vault is sparse, still surface whatever you can and note that the vault has more to offer as they add notes.
- The summary and unique_value_props should make the person feel genuinely proud when they read them.
"""


class ProfileBuilder:
    def __init__(self, config: AIConfig):
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model = config.model

    def build(
        self,
        resume_data: Dict,
        vault_index=None,  # VaultIndex instance, optional
        user_overrides: Optional[Dict] = None,
    ) -> UserProfile:
        """
        Build a UserProfile from resume + optional vault index.

        Args:
            resume_data:   Output from resume_parser.parse_resume()
            vault_index:   VaultIndex instance (optional). When provided, the
                           profile synthesis receives a compact index overview
                           (~2KB) plus full content of work/project/skill notes
                           (~15KB). This replaces the old 50KB random text dump.
            user_overrides: Dict of profile fields to force-set (name, email, etc.)
        """
        print("[profile] Building unified profile with Claude...")

        resume_text = resume_data.get("raw_text", "")
        vault_section = self._build_vault_section(vault_index)
        index_overview = ""
        if vault_index is not None:
            index_overview = vault_index.get_index_overview()

        user_content = f"""## RESUME / WORK HISTORY
{resume_text}

## VAULT INDEX (tags + summaries for every note — read this first to understand scope)
{index_overview if index_overview else "(No vault provided)"}

## VAULT CONTENT (full text of work, project, and skill notes)
{vault_section}
"""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=PROFILE_SYNTHESIS_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )

        raw = response.content[0].text.strip()

        # Extract JSON — handle markdown code fences or bare JSON
        json_match = re.search(r'\{[\s\S]*\}', raw)
        raw_json = json_match.group(0) if json_match else raw

        try:
            profile_data = json.loads(raw_json)
        except json.JSONDecodeError as e:
            print(f"[profile] JSON parse failed ({e}), trying truncation recovery...")
            # Walk backwards from the end to find the outermost closing brace
            recovered = False
            for i in range(len(raw_json) - 1, -1, -1):
                if raw_json[i] == '}':
                    try:
                        profile_data = json.loads(raw_json[:i + 1])
                        print(f"[profile] Recovered truncated JSON at position {i}")
                        recovered = True
                        break
                    except json.JSONDecodeError:
                        continue
            if not recovered:
                print(f"[profile] WARNING: Could not parse JSON. Raw response snippet:\n{raw[:500]}")
                contact = resume_data.get("contact", {})
                profile_data = {
                    "name": contact.get("name", ""),
                    "email": contact.get("email", ""),
                    "phone": contact.get("phone", ""),
                    "location": contact.get("location", ""),
                    "linkedin_url": contact.get("linkedin", ""),
                    "summary": "",
                    "unique_value_props": [],
                    "skills": [],
                    "certifications": [],
                    "vault_insights": {},
                }

        # Build UserProfile from parsed data
        profile = UserProfile(
            name=profile_data.get("name", ""),
            email=profile_data.get("email", ""),
            phone=profile_data.get("phone", ""),
            location=profile_data.get("location", ""),
            linkedin_url=profile_data.get("linkedin_url", ""),
            summary=profile_data.get("summary", ""),
            unique_value_props=profile_data.get("unique_value_props", []),
            skills=profile_data.get("skills", []),
            vault_skills=profile_data.get("vault_skills", []),
            vault_gems=profile_data.get("vault_gems", []),
            certifications=profile_data.get("certifications", []),
            vault_insights=profile_data.get("vault_insights", {}),
            raw_resume_text=resume_text,
            raw_vault_text=index_overview,
        )

        # Parse experience
        for exp in profile_data.get("experience", []):
            profile.experience.append(WorkExperience(
                title=exp.get("title", ""),
                company=exp.get("company", ""),
                start_date=exp.get("start_date"),
                end_date=exp.get("end_date"),
                description=exp.get("description", ""),
                achievements=exp.get("achievements", []),
                skills_used=exp.get("skills_used", []),
            ))

        # Parse education
        for edu in profile_data.get("education", []):
            profile.education.append(Education(
                degree=edu.get("degree", ""),
                school=edu.get("school", ""),
                field=edu.get("field"),
                year=edu.get("year"),
            ))

        # Parse projects
        profile.projects = profile_data.get("projects", [])
        gem_count = len(profile.vault_gems)
        vs_count = len(profile.vault_skills)
        print(f"[profile] Vault analysis complete: {gem_count} hidden gems, {vs_count} vault-only skills surfaced")

        # Apply user overrides (explicit config values win)
        if user_overrides:
            for key, value in user_overrides.items():
                if value and hasattr(profile, key):
                    setattr(profile, key, value)

        # Fill from resume contact if still empty
        contact = resume_data.get("contact", {})
        if not profile.email and contact.get("email"):
            profile.email = contact["email"]
        if not profile.phone and contact.get("phone"):
            profile.phone = contact["phone"]
        if not profile.linkedin_url and contact.get("linkedin"):
            profile.linkedin_url = contact["linkedin"]

        print(f"[profile] Built profile: {len(profile.experience)} roles, "
              f"{len(profile.skills)} skills, {len(profile.projects)} projects")
        return profile

    def _build_vault_section(self, vault_index, max_chars: int = 18000) -> str:
        """
        Return full note content for work / project / skill notes.
        Used only during profile synthesis (once per session).
        Cap at max_chars to stay well within the Opus context window.
        """
        if vault_index is None:
            return "(No vault provided — using resume only)"
        content = vault_index.get_category_content(
            categories=["work", "project", "skill"],
            max_files=20,
            max_chars_per_file=2000,
            max_total_chars=max_chars,
        )
        if content:
            print(f"[profile] Vault content: {len(content):,} chars "
                  f"(indexed, work/project/skill notes only)")
        return content or "(No work/project/skill notes found in vault)"

    def build_from_text(self, resume_text: str, vault_text: str = "") -> UserProfile:
        """Convenience method that accepts raw text (no vault index available)."""
        resume_data = {"raw_text": resume_text, "contact": {}, "sections": {}}
        return self.build(resume_data, vault_index=None)
