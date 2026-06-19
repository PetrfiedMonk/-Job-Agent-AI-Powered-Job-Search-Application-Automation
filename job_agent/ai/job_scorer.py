"""
Job Scorer
Uses Claude to score each job posting against the user's profile.
Rates: skill match, experience fit, salary potential, and role alignment.
Filters out poor fits early to avoid wasting apply cycles.
"""
import json
from typing import List, Dict
import anthropic

from job_agent.models import JobPosting, UserProfile
from job_agent.config import AIConfig


SCORING_SYSTEM_PROMPT = """You are a recruiter and career coach evaluating job-candidate fit.
Score each job posting against the candidate's profile. Be honest and realistic.

Return ONLY a JSON array (no explanation) with one object per job:
[
  {
    "job_id": "id from input",
    "fit_score": 85,           // 0-100: how well the candidate matches requirements
    "salary_score": 70,        // 0-100: likely meets/exceeds min salary (100 = well above min)
    "combined_score": 80,      // weighted: fit_score*0.6 + salary_score*0.4
    "match_reasons": ["Strong PM background", "Python + AI skills match"],
    "gap_reasons": ["Lacks healthcare industry exp"],
    "recommended_keywords": ["product roadmap", "stakeholder management", "agile"],
    "apply": true              // false if score < 50 or obvious mismatch
  }
]
"""


class JobScorer:
    def __init__(self, config: AIConfig):
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.model = config.resume_model  # Use faster model for scoring

    def score_batch(
        self, jobs: List[JobPosting], profile: UserProfile, min_score: float = 50.0
    ) -> List[JobPosting]:
        """
        Score a batch of jobs against the user's profile.
        Updates job.fit_score, salary_score, combined_score in place.
        Returns jobs sorted by combined_score, filtered by min_score.
        """
        if not jobs:
            return []

        print(f"[scorer] Scoring {len(jobs)} jobs against profile...")

        # Batch into groups of 10 for efficiency
        scored = []
        for i in range(0, len(jobs), 10):
            batch = jobs[i:i + 10]
            scored.extend(self._score_batch(batch, profile))

        # Filter and sort
        qualified = [j for j in scored if j.combined_score >= min_score]
        qualified.sort(key=lambda j: j.combined_score, reverse=True)

        print(f"[scorer] {len(qualified)}/{len(scored)} jobs scored >= {min_score}")
        return qualified

    def _score_batch(self, jobs: List[JobPosting], profile: UserProfile) -> List[JobPosting]:
        jobs_payload = []
        for job in jobs:
            jobs_payload.append({
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "salary": job.salary_display,
                "description": job.description[:2000],  # Trim for context
            })

        profile_summary = f"""
Candidate: {profile.name}
Target roles: {', '.join(profile.target_roles or ['Product Manager', 'Business Analyst'])}
Min salary: ${profile.min_salary:,}
Skills: {', '.join(profile.skills[:40])}
Experience summary: {profile.summary}
Years of experience: {len(profile.experience)} roles
Certifications: {', '.join(profile.certifications)}
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SCORING_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"CANDIDATE PROFILE:\n{profile_summary}\n\nJOBS TO SCORE:\n{json.dumps(jobs_payload, indent=2)}"
                }],
            )

            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip().rstrip("```")

            scores: List[Dict] = json.loads(raw)
            score_map = {s["job_id"]: s for s in scores}

            for job in jobs:
                if job.id in score_map:
                    s = score_map[job.id]
                    job.fit_score = float(s.get("fit_score", 0))
                    job.salary_score = float(s.get("salary_score", 0))
                    job.combined_score = float(s.get("combined_score", 0))
                    job.score_breakdown = {
                        "match_reasons": s.get("match_reasons", []),
                        "gap_reasons": s.get("gap_reasons", []),
                        "recommended_keywords": s.get("recommended_keywords", []),
                        "apply": s.get("apply", True),
                    }

        except Exception as e:
            print(f"[scorer] Warning: scoring batch failed: {e}")
            # Fall back to neutral scores so jobs aren't dropped
            for job in jobs:
                job.fit_score = 60.0
                job.salary_score = 60.0
                job.combined_score = 60.0

        return jobs
