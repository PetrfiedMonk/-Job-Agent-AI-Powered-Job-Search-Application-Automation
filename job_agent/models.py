"""
Core data models for the Job Agent system.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class ApplicationStatus(str, Enum):
    QUEUED = "queued"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"           # Automation crashed/errored — can retry
    NEEDS_MANUAL = "needs_manual"  # Tried but requires human (CAPTCHA, login, complex ATS)
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"


class JobPlatform(str, Enum):
    INDEED = "indeed"
    LINKEDIN = "linkedin"
    COMPANY = "company"
    ZIPRECRUITER = "ziprecruiter"
    GLASSDOOR = "glassdoor"


@dataclass
class JobPosting:
    id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    platform: JobPlatform
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_text: Optional[str] = None
    job_type: Optional[str] = None
    remote: bool = False
    posted_date: Optional[datetime] = None
    easy_apply: bool = False          # LinkedIn Easy Apply / Indeed Apply
    description_summary: str = ""    # Compressed description (~700 chars), stored in DB
    fit_score: float = 0.0            # 0-100, AI-scored match
    salary_score: float = 0.0         # 0-100, compensation potential
    combined_score: float = 0.0       # weighted final score
    score_breakdown: Dict[str, Any] = field(default_factory=dict)

    @property
    def salary_display(self) -> str:
        if self.salary_text:
            return self.salary_text
        if self.salary_min and self.salary_max:
            return f"${self.salary_min:,} - ${self.salary_max:,}"
        if self.salary_min:
            return f"${self.salary_min:,}+"
        return "Not listed"


@dataclass
class WorkExperience:
    title: str
    company: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: str = ""
    achievements: List[str] = field(default_factory=list)
    skills_used: List[str] = field(default_factory=list)


@dataclass
class Education:
    degree: str
    school: str
    field: Optional[str] = None
    year: Optional[str] = None


@dataclass
class UserProfile:
    """Unified profile synthesized from resume + Obsidian vault."""
    # Core identity
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    website: str = ""

    # Professional content
    summary: str = ""                              # AI-synthesized narrative
    experience: List[WorkExperience] = field(default_factory=list)
    education: List[Education] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)
    projects: List[Dict[str, Any]] = field(default_factory=list)

    # Vault-enriched content
    vault_insights: Dict[str, str] = field(default_factory=dict)   # topic -> summary
    unique_value_props: List[str] = field(default_factory=list)    # USPs from vault analysis
    vault_skills: List[str] = field(default_factory=list)          # Skills found only in vault
    vault_gems: List[Dict[str, str]] = field(default_factory=list) # Hidden strengths discovered
    raw_vault_text: str = ""                                        # Full vault dump for AI context

    # Job preferences
    target_roles: List[str] = field(default_factory=list)
    target_industries: List[str] = field(default_factory=list)
    min_salary: int = 80000
    preferred_locations: List[str] = field(default_factory=list)
    remote_preference: str = "hybrid"   # remote, hybrid, onsite

    # Raw source data
    raw_resume_text: str = ""


@dataclass
class TailoredResume:
    job: JobPosting
    profile: UserProfile
    tailored_summary: str = ""
    tailored_experience: List[WorkExperience] = field(default_factory=list)
    highlighted_skills: List[str] = field(default_factory=list)
    keywords_matched: List[str] = field(default_factory=list)
    ats_score_estimate: float = 0.0
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    generated_at: datetime = field(default_factory=datetime.now)


@dataclass
class Application:
    id: str
    job: JobPosting
    resume: TailoredResume
    status: ApplicationStatus = ApplicationStatus.QUEUED
    applied_at: Optional[datetime] = None
    interview_at: Optional[datetime] = None
    notes: str = ""
    error: str = ""
    form_data: Dict[str, str] = field(default_factory=dict)  # field -> value cache
