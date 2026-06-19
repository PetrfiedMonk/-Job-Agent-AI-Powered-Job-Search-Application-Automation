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
from typing import List
import anthropic

from job_agent.models import (
    JobPosting, UserProfile, TailoredResume, WorkExperience
)
from job_agent.config import AIConfig


TAILOR_SYSTEM_PROMPT = """You are an elite resume writer who specializes in tailoring resumes for maximum ATS scores and interview conversion.

Given a job posting and a candidate's full profile, produce a tailored resume that:
1. MIRRORS the exact language, keywords, and phrases from the job description
2. Leads with the most relevant experience for THIS specific role
3. Rewrites experience bullets to emphasize outcomes directly relevant to the job
4. Surfaces relevant vault/project knowledge the candidate may have overlooked
5. Is optimized for ATS systems (proper keywords, clean formatting)
6. Has a powerful summary paragraph that directly addresses what the employer wants

Return ONLY a JSON object:
{
  "tailored_summary": "2-3 sentence summary written to directly address THIS job's needs",
  "experience": [
    {
      "title": "Job Title",
      "company": "Company",
      "start_date": "2020",
      "end_date": "Present",
      "description": "Brief description",
      "achievements": [
        "Rewritten bullet that mirrors job language and quantifies impact",
        "Another strong achievement bullet (use numbers wherever possible)"
      ],
      "skills_used": ["relevant skill 1", "relevant skill 2"]
    }
  ],
  "highlighted_skills": ["skill1", "skill2"],  // Top 15 skills most relevant to this job
  "keywords_matched": ["keyword from JD that appears in resume"],
  "ats_score_estimate": 85,   // 0-100 estimated ATS match score
  "cover_letter_opening": "Optional: 2-sentence cover letter opener"
}

RULES:
- Only include the 3-5 most relevant work experiences (not all of them)
- Each experience should have 3-5 achievement bullets
- Use numbers/metrics wherever possible or inferable
- Do NOT invent jobs, companies, or degrees
- Do NOT add skills the candidate clearly doesn't have
- Mirror the JOB's exact terminology (if they say "roadmap", use "roadmap")
"""


class ResumeTailor:
    def __init__(self, config: AIConfig):
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model = config.resume_model

    def tailor(self, job: JobPosting, profile: UserProfile) -> TailoredResume:
        """
        Generate a tailored resume for a specific job posting.
        """
        print(f"[tailor] Tailoring resume for: {job.title} @ {job.company}")

        # Build rich context
        keywords = job.score_breakdown.get("recommended_keywords", [])
        profile_context = self._build_profile_context(profile)

        prompt = f"""JOB POSTING:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Salary: {job.salary_display}

JOB DESCRIPTION:
{job.description}

KEYWORDS TO TARGET: {', '.join(keywords) if keywords else 'Extract from job description'}

---

CANDIDATE PROFILE:
{profile_context}

---

VAULT / ADDITIONAL CONTEXT:
{profile.raw_vault_text[:8000] if profile.raw_vault_text else '(No additional vault context)'}

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
