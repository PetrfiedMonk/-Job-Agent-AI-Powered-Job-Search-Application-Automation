"""
Core data models for the Job Agent system.
"""
import asyncio
import threading
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, TYPE_CHECKING
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
    location: str = ""          # "City, ST" display string
    address_line1: str = ""     # Street address for application forms
    address_line2: str = ""     # Apt/Suite (optional)
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = "United States"
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
    cover_letter_text: str = ""      # Generated cover letter body; empty = not generated
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    generated_at: datetime = field(default_factory=datetime.now)


class LazyResume:
    """
    Drop-in replacement for TailoredResume that defers all AI generation
    until the application agent actually needs the content.

    Trigger chain (each step implies the previous):
      file upload detected  → ensure_docx()        → ensure_tailored() internally
      cover letter field    → ensure_cover_letter() → ensure_tailored() internally
      open-ended question   → ensure_tailored()

    All attribute reads work unchanged from agent code — they just return
    empty defaults until the relevant ensure_*() is awaited.
    """

    def __init__(
        self,
        job: "JobPosting",
        profile: "UserProfile",
        tailor,
        resumes_dir: str,
        auto_cover_letter: bool = False,
        vault_index=None,
    ):
        self.job = job
        self.profile = profile
        self._tailor = tailor
        self._resumes_dir = resumes_dir
        self._auto_cover_letter = auto_cover_letter
        self._vault_index = vault_index

        # Generation state
        self._tailored: Optional[TailoredResume] = None
        self._tailoring = False     # guard against re-entrant async calls
        self._cl_done = False
        self._docx_done = False
        self._lock = threading.Lock()

        # TailoredResume-compatible interface — defaults until materialised
        self.tailored_summary: str = ""
        self.tailored_experience: list = []
        self.highlighted_skills: list = []
        self.keywords_matched: list = []
        self.ats_score_estimate: float = 0.0
        self.cover_letter_text: str = ""
        self.docx_path: Optional[str] = None
        self.pdf_path: Optional[str] = None
        self.generated_at: datetime = datetime.now()

    # ── internal sync worker (runs in executor thread) ────────────────────────

    def _run_tailor(self) -> Optional[TailoredResume]:
        return self._tailor.tailor(self.job, self.profile, vault_index=self._vault_index)

    def _run_cover_letter(self) -> str:
        return self._tailor.generate_cover_letter(self.job, self.profile)

    def _run_build_docx(self) -> None:
        from job_agent.builders.resume_builder import build_resume_docx
        build_resume_docx(self._tailored, self._resumes_dir)

    # ── public async API ──────────────────────────────────────────────────────

    async def ensure_tailored(self) -> bool:
        """
        Generate tailored resume content (summary, experience bullets, skills).
        Runs in a thread pool so the event loop stays responsive.
        Returns True if generation happened, False if already done or failed.
        """
        if self._tailored is not None:
            return False
        with self._lock:
            if self._tailored is not None:
                return False
        print(f"[lazy-resume] Tailoring on-demand: {self.job.title} @ {self.job.company}")
        loop = asyncio.get_event_loop()
        try:
            t = await loop.run_in_executor(None, self._run_tailor)
            self._tailored = t
            self.tailored_summary = t.tailored_summary
            self.tailored_experience = t.tailored_experience
            self.highlighted_skills = t.highlighted_skills
            self.keywords_matched = t.keywords_matched
            self.ats_score_estimate = t.ats_score_estimate
            print(f"[lazy-resume] Tailored ✓ — {self.job.title}")
            return True
        except Exception as e:
            print(f"[lazy-resume] Tailor failed ({self.job.title}): {e}")
            return False

    async def ensure_docx(self) -> bool:
        """
        Build a tailored DOCX resume file.
        Triggers ensure_tailored() first if content hasn't been generated yet.
        Returns True if a new file was built.
        """
        if self.docx_path:
            return False
        await self.ensure_tailored()
        if self._tailored is None:
            print(f"[lazy-resume] Skipping DOCX — tailor failed: {self.job.title}")
            return False
        print(f"[lazy-resume] Building DOCX on-demand: {self.job.title}")
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._run_build_docx)
            self.docx_path = self._tailored.docx_path
            print(f"[lazy-resume] DOCX ready ✓ — {self.docx_path}")
            return True
        except Exception as e:
            print(f"[lazy-resume] DOCX build failed ({self.job.title}): {e}")
            return False

    async def ensure_cover_letter(self) -> bool:
        """
        Generate a tailored cover letter.
        Triggers ensure_tailored() first so the CL has full job context.
        Returns True if a new letter was generated.
        """
        if self.cover_letter_text:
            return False
        await self.ensure_tailored()
        print(f"[lazy-resume] Generating cover letter on-demand: {self.job.title}")
        loop = asyncio.get_event_loop()
        try:
            cl = await loop.run_in_executor(None, self._run_cover_letter)
            self.cover_letter_text = cl
            self._cl_done = True
            print(f"[lazy-resume] Cover letter ready ✓ — {self.job.title}")
            return True
        except Exception as e:
            print(f"[lazy-resume] Cover letter failed ({self.job.title}): {e}")
            return False


@dataclass
class Application:
    id: str
    job: JobPosting
    resume: Any  # TailoredResume or LazyResume
    status: ApplicationStatus = ApplicationStatus.QUEUED
    applied_at: Optional[datetime] = None
    interview_at: Optional[datetime] = None
    notes: str = ""
    error: str = ""
    form_data: Dict[str, str] = field(default_factory=dict)  # field -> value cache
