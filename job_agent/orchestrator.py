"""
Orchestrator
The main pipeline that ties everything together:
  1. Load & parse Obsidian vault + resume
  2. Build unified AI profile
  3. Search for jobs across platforms
  4. Score and rank jobs
  5. Tailor resume for each top job
  6. Generate DOCX resumes
  7. Auto-apply via Playwright
  8. Track everything in SQLite
"""
import uuid
import time
import json
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from job_agent.config import AppConfig, load_config
from job_agent.models import Application, ApplicationStatus, TailoredResume, UserProfile, WorkExperience, Education
from job_agent.parsers.vault_parser import VaultParser
from job_agent.parsers.vault_index import VaultIndex
from job_agent.parsers.resume_parser import parse_resume
from job_agent.ai.profile_builder import ProfileBuilder
from job_agent.ai.job_scorer import JobScorer
from job_agent.ai.resume_tailor import ResumeTailor
from job_agent.builders.resume_builder import build_resume_docx
from job_agent.search.job_searcher import JobSearcher
from job_agent.automation.application_agent import ApplicationAgent
from job_agent.db.tracker import Tracker

PROFILE_CACHE_FILE = "profile_cache.json"


class JobOrchestrator:
    def __init__(self, config: AppConfig):
        self.config = config
        self._setup_output_dirs()

        self.tracker = Tracker(config.output.db_path)
        self.searcher = JobSearcher(config.search)
        self.scorer = JobScorer(config.ai)
        self.tailor = ResumeTailor(config.ai)
        self.agent = ApplicationAgent(config.automation, config.output.screenshots_dir)

        self.profile = None      # Loaded lazily
        self.vault_index = None  # Built lazily alongside profile

    def _setup_output_dirs(self):
        for d in [
            self.config.output.output_dir,
            self.config.output.resumes_dir,
            self.config.output.screenshots_dir,
        ]:
            Path(d).mkdir(parents=True, exist_ok=True)

    # -- Profile Loading --

    def load_profile(self, force_rebuild: bool = False):
        """Load the user profile, using disk cache when available."""
        if self.profile and not force_rebuild:
            return self.profile

        # Try the disk cache first (skips Claude API call)
        if not force_rebuild:
            cached = self._load_profile_cache()
            if cached:
                self.profile = cached
                # Also load vault index so tailoring still works
                self._load_vault_index()
                return self.profile

        print("\n[orchestrator] -- STEP 1: Building Profile --")
        self.profile = self._build_profile()
        self._save_profile_cache(self.profile)
        return self.profile

    def _build_profile(self) -> "UserProfile":
        """Call Claude to synthesize a fresh profile from resume + vault."""
        # Parse resume
        resume_data = {}
        if self.config.profile.resume_path:
            print(f"[orchestrator] Parsing resume: {self.config.profile.resume_path}")
            resume_data = parse_resume(self.config.profile.resume_path)
        else:
            print("[orchestrator] No resume path set. Using empty resume.")
            resume_data = {"raw_text": "", "contact": {}, "sections": {}}

        vault_index = self._load_vault_index(force=True)

        builder = ProfileBuilder(self.config.ai)
        user_overrides = {
            "name": self.config.profile.name,
            "email": self.config.profile.email,
            "phone": self.config.profile.phone,
            "location": self.config.profile.location,
            "linkedin_url": self.config.profile.linkedin_url,
            "website": self.config.profile.website,
            "min_salary": self.config.search.min_salary,
            "target_roles": self.config.search.keywords[:5],
        }
        profile = builder.build(resume_data, vault_index, user_overrides)
        print(f"[orchestrator] Profile built for: {profile.name}")
        return profile

    def _load_vault_index(self, force: bool = False) -> Optional[VaultIndex]:
        """Load (or rebuild) the vault index. No Claude calls — pure local parsing."""
        if self.vault_index and not force:
            return self.vault_index
        if not self.config.profile.obsidian_vault_path:
            print("[orchestrator] No Obsidian vault path configured.")
            return None
        print(f"[orchestrator] Indexing Obsidian vault: {self.config.profile.obsidian_vault_path}")
        try:
            vault_index = VaultIndex(
                self.config.profile.obsidian_vault_path,
                index_dir=self.config.output.output_dir,
            )
            vault_index.build(force=force)
            self.vault_index = vault_index
            return vault_index
        except Exception as e:
            print(f"[orchestrator] Warning: vault index failed: {e}")
            return None

    # -- Profile Cache --

    def _profile_cache_path(self) -> Path:
        return Path(self.config.output.output_dir) / PROFILE_CACHE_FILE

    def _save_profile_cache(self, profile: UserProfile):
        try:
            data = asdict(profile)
            data["_cached_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(self._profile_cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[orchestrator] Profile saved to cache: {self._profile_cache_path()}")
        except Exception as e:
            print(f"[orchestrator] Warning: could not save profile cache: {e}")

    def _load_profile_cache(self) -> Optional[UserProfile]:
        path = self._profile_cache_path()
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            cached_at = data.pop("_cached_at", "unknown")
            experience = [WorkExperience(**e) for e in data.pop("experience", [])]
            education = [Education(**e) for e in data.pop("education", [])]
            profile = UserProfile(**data)
            profile.experience = experience
            profile.education = education
            print(f"[orchestrator] Profile loaded from cache (built: {cached_at})")
            return profile
        except Exception as e:
            print(f"[orchestrator] Warning: could not load profile cache: {e}")
            return None

    # -- Main Pipeline --

    def run(
        self,
        search_only: bool = False,
        apply_only: bool = False,
        max_apply: Optional[int] = None,
        min_score: float = 65.0,
    ):
        """
        Run the full job agent pipeline.

        Args:
            search_only: Only search and score, don't apply
            apply_only: Skip search, only apply to queued jobs
            max_apply: Override max_applications_per_run
            min_score: Minimum combined score to apply (0-100)
        """
        print("\n" + "="*60)
        print("  JOB AGENT — Starting Pipeline")
        print("="*60)
        start_time = time.time()

        # Step 1: Profile
        profile = self.load_profile()

        if not apply_only:
            # Step 2: Search
            print("\n[orchestrator] -- STEP 2: Searching Jobs --")
            jobs = self.searcher.search_all()
            print(f"[orchestrator] Found {len(jobs)} jobs before scoring")

            # Step 3: Score
            print("\n[orchestrator] -- STEP 3: Scoring Jobs --")
            scored_jobs = self.scorer.score_batch(jobs, profile, min_score=min_score)
            print(f"[orchestrator] {len(scored_jobs)} jobs scored >= {min_score}")

            # Save to DB
            new_count = 0
            for job in scored_jobs:
                if self.tracker.upsert_job(job):
                    new_count += 1
            print(f"[orchestrator] {new_count} new jobs saved to database")

        if search_only:
            print("\n[orchestrator] Search-only mode. Skipping apply.")
            self.tracker.print_dashboard()
            return

        # Step 4: Tailor + Apply
        print("\n[orchestrator] ── STEP 4: Tailoring & Applying ──")
        top_jobs_data = self.tracker.get_jobs(min_score=min_score, limit=50)

        apply_limit = max_apply or self.config.automation.max_applications_per_run
        applications: List[Application] = []

        for job_data in top_jobs_data:
            if len(applications) >= apply_limit:
                break

            job_id = job_data["id"]
            if self.tracker.already_applied(job_id):
                continue

            # Reconstruct JobPosting from DB row
            from job_agent.models import JobPosting, JobPlatform
            job = JobPosting(
                id=job_data["id"],
                title=job_data["title"],
                company=job_data["company"],
                location=job_data["location"] or "",
                description=job_data["description"] or "",
                url=job_data["url"] or "",
                platform=JobPlatform(job_data["platform"]) if job_data["platform"] else JobPlatform.INDEED,
                salary_min=job_data["salary_min"],
                salary_max=job_data["salary_max"],
                fit_score=job_data["fit_score"] or 0,
                combined_score=job_data["combined_score"] or 0,
            )

            # Tailor resume (vault_index enables targeted retrieval per job)
            try:
                tailored = self.tailor.tailor(job, profile, vault_index=self.vault_index)
            except Exception as e:
                print(f"[orchestrator] Resume tailor failed for {job.title}: {e}")
                continue

            # Build DOCX
            try:
                build_resume_docx(tailored, self.config.output.resumes_dir)
            except Exception as e:
                print(f"[orchestrator] DOCX build failed: {e}")
                # Continue even without DOCX - can still fill forms

            # Create application record
            app = Application(
                id=str(uuid.uuid4()),
                job=job,
                resume=tailored,
                status=ApplicationStatus.QUEUED,
            )
            self.tracker.create_application(app)
            applications.append(app)

        print(f"[orchestrator] Prepared {len(applications)} applications")

        if applications:
            print(f"\n[orchestrator] ── STEP 5: Submitting Applications ──")
            if not self.config.automation.auto_submit:
                print("[orchestrator] ⚠️  auto_submit=False — forms will be filled but NOT submitted")
                print("[orchestrator]    Set auto_submit=True in config.yaml when ready to go live")

            results = self.agent.apply_batch(applications)

            # Sync results to DB
            for app in results:
                self.tracker.sync_application(app)

            applied = sum(1 for a in results if a.status == ApplicationStatus.APPLIED)
            failed = sum(1 for a in results if a.status == ApplicationStatus.FAILED)
            print(f"\n[orchestrator] Applied: {applied} | Failed: {failed}")

        elapsed = time.time() - start_time
        print(f"\n[orchestrator] ── Pipeline complete in {elapsed:.0f}s ──")
        self.tracker.print_dashboard()

    # ── Convenience methods ───────────────────────────────────────────────────

    def search_and_score(self, min_score: float = 65.0):
        """Just search and score — no applying."""
        self.run(search_only=True, min_score=min_score)

    def apply_queued(self, max_apply: int = 10):
        """Apply to jobs already queued in DB."""
        self.run(apply_only=True, max_apply=max_apply)

    def dashboard(self):
        """Print current pipeline stats."""
        self.tracker.print_dashboard()
