"""
Profile Builder
Uses Claude to synthesize your Obsidian vault + resume into a rich,
structured UserProfile that powers all downstream resume tailoring.
"""
import json
from typing import Dict, Optional
import anthropic

from job_agent.models import UserProfile, WorkExperience, Education
from job_agent.config import AIConfig
from job_agent.parsers.vault_parser import get_vault_summary_for_ai


PROFILE_SYNTHESIS_PROMPT = """You are a professional resume writer and career coach analyzing a person's complete work history and personal knowledge base.

I'm going to give you:
1. Their current resume/work history
2. Content from their personal Obsidian knowledge base (notes, ideas, projects, reflections)

Your job is to synthesize ALL of this into a rich, structured profile. Mine the vault deeply for:
- Hidden skills and expertise they forgot to put on their resume
- Project details that demonstrate impact
- Technical knowledge demonstrated through their notes
- Unique perspectives and value propositions
- Achievements that could be quantified

Return a JSON object with this exact structure:
{
  "name": "Full Name",
  "email": "email@example.com",
  "phone": "555-555-5555",
  "location": "City, State",
  "linkedin_url": "",
  "summary": "3-4 sentence professional narrative that highlights their unique value, written in first person",
  "unique_value_props": [
    "Specific differentiator 1 (e.g., 'Built and launched SaaS product solo - knows full product lifecycle')",
    "Specific differentiator 2",
    "Specific differentiator 3"
  ],
  "skills": ["skill1", "skill2", ...],  // Comprehensive list from both resume and vault
  "certifications": ["cert1", "cert2"],
  "experience": [
    {
      "title": "Job Title",
      "company": "Company Name",
      "start_date": "2020",
      "end_date": "Present",
      "description": "Brief role description",
      "achievements": [
        "Quantified achievement 1 (add numbers where inferable)",
        "Quantified achievement 2"
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
      "description": "What it does and why it matters",
      "tech_stack": ["tech1", "tech2"],
      "url": "",
      "impact": "Measurable result or significance"
    }
  ],
  "vault_insights": {
    "topic_name": "Key insight or capability demonstrated in vault notes"
  }
}

IMPORTANT:
- Be specific and quantified wherever possible
- Surface skills/knowledge from the vault that aren't on the resume
- Write the summary to maximize interview conversion
- Don't invent facts - only use what's provided
"""


class ProfileBuilder:
    def __init__(self, config: AIConfig):
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model = config.model

    def build(
        self,
        resume_data: Dict,
        vault_data: Optional[Dict] = None,
        user_overrides: Optional[Dict] = None,
    ) -> UserProfile:
        """
        Build a UserProfile from resume + optional vault data.

        Args:
            resume_data: Output from resume_parser.parse_resume()
            vault_data: Output from vault_parser.VaultParser.parse() (optional)
            user_overrides: Dict of profile fields to force-set (name, email, etc.)
        """
        print("[profile] Building unified profile with Claude...")

        # Prepare context
        resume_text = resume_data.get("raw_text", "")
        vault_summary = ""
        if vault_data:
            vault_summary = get_vault_summary_for_ai(vault_data)
            print(f"[profile] Including {len(vault_summary):,} chars of vault context")

        user_content = f"""## RESUME / WORK HISTORY
{resume_text}

## OBSIDIAN VAULT CONTENT
{vault_summary if vault_summary else "(No vault provided - using resume only)"}
"""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=PROFILE_SYNTHESIS_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )

        raw_json = response.content[0].text
        # Strip markdown code fences if present
        raw_json = raw_json.strip()
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        raw_json = raw_json.strip().rstrip("```")

        profile_data = json.loads(raw_json)

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
            certifications=profile_data.get("certifications", []),
            vault_insights=profile_data.get("vault_insights", {}),
            raw_resume_text=resume_text,
            raw_vault_text=vault_summary,
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

    def build_from_text(self, resume_text: str, vault_text: str = "") -> UserProfile:
        """Convenience method that accepts raw text directly."""
        resume_data = {"raw_text": resume_text, "contact": {}, "sections": {}}
        if vault_text:
            vault_data = {
                "notes": [],
                "by_category": {},
                "all_text": vault_text,
                "skills_mentioned": [],
                "companies_mentioned": [],
                "summary_stats": {},
            }
        else:
            vault_data = None
        return self.build(resume_data, vault_data)
