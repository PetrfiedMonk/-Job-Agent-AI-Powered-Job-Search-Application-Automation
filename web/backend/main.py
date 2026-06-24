"""
Job Agent Web Server - FastAPI Backend
Provides REST API and WebSocket endpoints for the React frontend
"""

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sys
import os

# Force UTF-8 on stdout/stderr so emoji in print() don't crash on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add parent directory to path to import job_agent
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_agent.config import load_config
from job_agent.orchestrator import JobOrchestrator
from job_agent.db.tracker import Tracker
from job_agent.db.field_semantics import FieldSemanticsDB
from job_agent.models import JobPosting, JobPlatform
from job_agent.ai.resume_tailor import ResumeTailor
from job_agent.ai.form_filler import SmartFormFiller
from job_agent.db.run_log import RunLog
from job_agent.ai.job_scorer import _location_allowed

# ── Logging Setup ──
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Data Models ──

class SearchSettings(BaseModel):
    keywords: list[str] = []
    locations: list[str] = []
    min_salary: int = 80000
    max_results: int = 25

class PipelineStatus(BaseModel):
    is_running: bool
    current_step: Optional[str] = None
    jobs_found: int = 0
    jobs_scored: int = 0
    jobs_applied: int = 0
    timestamp: str

class AssistLog(BaseModel):
    action_taken: str
    action_detail: str = ""

class JobResult(BaseModel):
    id: str
    title: str
    company: str
    location: str
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    url: str
    fit_score: float
    salary_score: float
    combined_score: float
    status: str
    date_found: str
    match_reasons: list[str] = []
    gap_reasons: list[str] = []
    recommended_keywords: list[str] = []
    tailored_summary: Optional[str] = None
    highlighted_skills: list[str] = []
    resume_path: Optional[str] = None
    vault_note_path: Optional[str] = None

# ── Global State ──

pipeline_running = False
current_step = None
agent = None
tracker = None
config = None
semantics_db = None
smart_filler = None
log_buffer = []
_run_log: Optional["RunLog"] = None  # initialized on first use
_stop_event = threading.Event()       # set = "stop requested"; checked between jobs

# CAPTCHA pause-gate (thread-safe): set=green, clear=waiting for human solve
_captcha_event = threading.Event()
_captcha_event.set()          # start in "no CAPTCHA" state
_captcha_job_info: dict = {}  # last CAPTCHA job context — shown in the UI
_assist_needed_info: dict = {}  # last NEEDS_MANUAL job context — shown in assist overlay
_main_loop: Optional[asyncio.AbstractEventLoop] = None

# ── Startup ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager"""
    global agent, tracker, config, semantics_db, smart_filler, _main_loop

    logger.info("Starting Job Agent Web Server...")
    _main_loop = asyncio.get_event_loop()
    try:
        config = load_config()
        agent = JobOrchestrator(config)
        tracker = Tracker()
        semantics_db = FieldSemanticsDB(config.output.db_path)
        smart_filler = SmartFormFiller(config.ai, semantics_db)

        # Wire the CAPTCHA pause-gate into the application agent
        agent.agent.captcha_event = _captcha_event
        agent.agent.captcha_notify_fn = _captcha_notify_fn
        # Wire field-memory lookup so the agent uses user-taught answers
        agent.agent.memory_lookup_fn = tracker.get_field_memory

        logger.info("Job Agent initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Job Agent: {e}")

    yield

    # ── Lifespan teardown (Ctrl+C / process kill) ──
    logger.info("Shutting down Job Agent Web Server — saving progress...")
    await _graceful_shutdown(source="lifespan")
    logger.info("Shutdown complete.")

# ── FastAPI App ──

app = FastAPI(
    title="Job Agent",
    description="AI-Powered Job Search & Application Automation",
    version="1.0.0",
    lifespan=lifespan
)

# ── CORS Middleware ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── REST Endpoints ──

@app.get("/api/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "agent_ready": agent is not None,
    }

@app.get("/api/profile")
async def get_profile():
    """Get the cached profile — never triggers a Claude build. Returns built:false if no cache exists."""
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        # Load from disk cache only — instant, no API calls
        profile = agent._load_profile_cache()
        if not profile:
            return {"built": False}

        vault_count = 0
        if agent.vault_index and agent.vault_index._data:
            vault_count = len(agent.vault_index._data.get("files", []))

        return {
            "built": True,
            "name": profile.name,
            "email": profile.email,
            "phone": profile.phone,
            "location": profile.location,
            "linkedin_url": profile.linkedin_url,
            "summary": profile.summary,
            "skills": profile.skills,
            "vault_skills": getattr(profile, "vault_skills", []),
            "vault_gems": getattr(profile, "vault_gems", []),
            "unique_value_props": profile.unique_value_props,
            "experience": [
                {
                    "title": e.title, "company": e.company,
                    "start_date": e.start_date, "end_date": e.end_date,
                    "description": e.description,
                    "achievements": e.achievements,
                }
                for e in (profile.experience or [])
            ],
            "education": [
                {"degree": e.degree, "school": e.school, "field": e.field, "year": e.year}
                for e in (profile.education or [])
            ],
            "projects": profile.projects or [],
            "vault_notes_count": vault_count,
        }
    except Exception as e:
        logger.error(f"Failed to load profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/status")
async def get_status() -> PipelineStatus:
    """Get current pipeline status"""
    jobs_found = 0
    jobs_scored = 0
    jobs_applied = 0
    
    if tracker:
        try:
            db_path = Path(config.output.output_dir) / "applications.db"
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()

                cursor.execute("SELECT COUNT(*) FROM jobs")
                jobs_found = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM jobs WHERE combined_score > 0")
                jobs_scored = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'applied'")
                jobs_applied = cursor.fetchone()[0]

                conn.close()
        except Exception as e:
            logger.warning(f"Failed to query database: {e}")
    
    return PipelineStatus(
        is_running=pipeline_running,
        current_step=current_step,
        jobs_found=jobs_found,
        jobs_scored=jobs_scored,
        jobs_applied=jobs_applied,
        timestamp=datetime.now().isoformat(),
    )

@app.get("/api/jobs")
async def get_jobs(status: Optional[str] = None, limit: int = 50) -> list[JobResult]:
    """Get jobs from database"""
    if not tracker:
        return []

    try:
        rows = tracker.get_jobs(min_score=0, limit=limit)
        jobs = []
        for row in rows:
            score_breakdown = {}
            if row.get("score_breakdown"):
                try:
                    score_breakdown = json.loads(row["score_breakdown"])
                except Exception:
                    pass

            app_status = row.get("app_status") or "found"
            if app_status == "dismissed":
                continue
            if status and app_status != status:
                continue

            skills_raw = row.get("highlighted_skills") or "[]"
            try:
                skills_list = json.loads(skills_raw) if isinstance(skills_raw, str) else (skills_raw or [])
            except Exception:
                skills_list = []

            jobs.append(JobResult(
                id=str(row["id"]),
                title=row.get("title") or "",
                company=row.get("company") or "",
                location=row.get("location") or "",
                salary_min=row.get("salary_min"),
                salary_max=row.get("salary_max"),
                url=row.get("url") or "",
                fit_score=float(row.get("fit_score") or 0),
                salary_score=float(row.get("salary_score") or 0),
                combined_score=float(row.get("combined_score") or 0),
                status=app_status,
                date_found=row.get("found_at") or "",
                match_reasons=score_breakdown.get("match_reasons", []),
                gap_reasons=score_breakdown.get("gap_reasons", []),
                recommended_keywords=score_breakdown.get("recommended_keywords", []),
                tailored_summary=row.get("tailored_summary") or None,
                highlighted_skills=skills_list,
                resume_path=row.get("resume_path") or None,
                vault_note_path=row.get("vault_note_path") or None,
            ))
        return jobs
    except Exception as e:
        logger.error(f"Failed to fetch jobs: {e}")
        return []

@app.post("/api/start-search")
async def start_search(settings: Optional[SearchSettings] = None):
    """Start a job search in the background"""
    global pipeline_running, current_step
    
    if pipeline_running:
        raise HTTPException(status_code=409, detail="Pipeline already running")
    
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    pipeline_running = True
    current_step = "Initializing..."
    
    # Run in background
    asyncio.create_task(run_search_background(settings))
    
    return {"status": "started", "message": "Job search started in background"}

@app.post("/api/stop-pipeline")
async def stop_pipeline():
    """Stop the running pipeline after the current job finishes."""
    global pipeline_running
    pipeline_running = False
    _stop_event.set()   # signals the job loop to exit after current job
    return {"status": "stopped"}


@app.post("/api/rescan-profile")
async def rescan_profile():
    """Force-rebuild the AI profile from scratch (re-reads vault + calls Claude).
    Use this after updating your resume or Obsidian notes."""
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    if pipeline_running:
        raise HTTPException(status_code=409, detail="Pipeline already running — try again after it finishes")
    try:
        profile = await asyncio.wait_for(
            asyncio.to_thread(agent.load_profile, True), timeout=120.0
        )
        return {
            "status": "ok",
            "name": profile.name,
            "skills_count": len(profile.skills),
            "message": "Profile rebuilt, cached, and saved to Obsidian vault. Next rebuild will use this as prior context.",
        }
    except Exception as e:
        logger.error(f"Profile rescan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cover-letter/{job_id}")
async def generate_cover_letter(job_id: str):
    """Generate a personalized cover letter for a specific job"""
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        db_path = Path(config.output.output_dir) / "applications.db"
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No jobs database found")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        job = JobPosting(
            id=str(row["id"]),
            title=row["title"],
            company=row["company"],
            location=row["location"] or "",
            description=row["description"] or "",
            url=row["url"] or "",
            platform=JobPlatform.INDEED,
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            salary_text=row["salary_text"],
        )

        profile = agent.load_profile()
        if not profile:
            raise HTTPException(status_code=503, detail="Profile not built — run a profile scan first")

        cover_letter = await asyncio.to_thread(
            agent.tailor.generate_cover_letter, job, profile
        )
        return {"job_id": job_id, "cover_letter": cover_letter}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cover letter generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Outreach Intel ──

OUTREACH_SYSTEM_PROMPT = """You are a job search strategist who thinks like an ethical social engineer — building genuine human connections at scale, fast.

Generate personalized outreach for a job seeker targeting a specific role. Be SPECIFIC to THIS company and role. No generic templates. Sound like a real person who actually did research.

Return ONLY valid JSON (no markdown fences):
{
  "linkedin_connection": "Under 300 chars. Personal connection request referencing something specific about this company, product, or role. Never start with 'I came across your listing'.",
  "linkedin_dm": "Post-connection DM. 2-3 short paragraphs. Acknowledge the connection, show you know their work or product, make a low-friction ask (15-min chat, not a job pitch).",
  "cold_email": {
    "subject": "Subject line that gets opened — specific and curious, never 'Interested in [Role]' or 'Following up'",
    "body": "3-4 tight paragraphs. 1: Specific hook about THEIR company/product (not the job listing). 2: One specific thing you have done that maps to their exact problem. 3: Social proof or a quick insight for them. 4: Low-friction ask."
  },
  "follow_up": {
    "subject": "Re: [original subject line]",
    "body": "2-3 sentences max. Adds new value or a new angle. Never 'just checking in' or apologetic."
  },
  "playbook": [
    "FORMAT each tactic as: [Channel]: [Specific action to take THIS WEEK]. Be concrete, not generic. Example: 'GitHub: Fork their repo acme/platform, fix a small open issue, submit a PR — your name appears in their contributor graph before you interview'",
    "Second tactic — different channel or method",
    "Third tactic",
    "Fourth tactic",
    "Fifth tactic — the most unconventional or creative one"
  ]
}"""


@app.post("/api/outreach/{job_id}")
async def generate_outreach(job_id: str):
    """Generate personalized outreach messages and a hacker playbook for a job."""
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        db_path = Path(config.output.output_dir) / "applications.db"
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No jobs database found")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        profile = agent.load_profile()
        if not profile:
            raise HTTPException(status_code=503, detail="Profile not built — run a profile scan first")

        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=config.ai.anthropic_api_key)

        top_role = (f"{profile.experience[0].title} at {profile.experience[0].company}"
                    if profile.experience else "N/A")

        prompt = f"""JOB: {row['title']} at {row['company']}
Location: {row['location'] or 'Not specified'}
Description (first 1500 chars):
{(row['description'] or '')[:1500]}

CANDIDATE:
Name: {profile.name}
Summary: {profile.summary}
Top Skills: {', '.join((profile.skills or [])[:10])}
Most Recent Role: {top_role}

Generate specific, personalized outreach for this exact role and company."""

        def _call_outreach():
            resp = client.messages.create(
                model=config.ai.scoring_model,
                max_tokens=2048,
                system=OUTREACH_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            # Robust JSON extraction — handle leading/trailing markdown or commentary
            m = re.search(r'\{[\s\S]*\}', raw)
            if not m:
                raise ValueError(f"No JSON object in outreach response: {raw[:200]}")
            return json.loads(m.group(0))

        data = await asyncio.to_thread(_call_outreach)
        data["job_id"] = job_id
        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Outreach generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs/{job_id}/brief")
async def get_job_brief(job_id: str):
    """
    Return a match brief for a job — instant, zero AI cost.
    Uses cached tailored_summary / highlighted_skills from the DB if present,
    falls back to raw profile data. No AI call on brief open.
    """
    if not agent or not tracker:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    rows = tracker.get_jobs(min_score=0, limit=500)
    row = next((r for r in rows if str(r["id"]) == str(job_id)), None)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    profile = agent._load_profile_cache() if not agent.profile else agent.profile
    if not profile:
        raise HTTPException(status_code=400, detail="No profile — run Deep Rescan first")

    score_breakdown: dict = {}
    if row.get("score_breakdown"):
        try:
            score_breakdown = json.loads(row["score_breakdown"])
        except Exception:
            pass

    job = JobPosting(
        id=job_id,
        title=row.get("title", ""),
        company=row.get("company", ""),
        location=row.get("location") or "",
        description=row.get("description") or "",
        url=row.get("url") or "",
        platform=JobPlatform(row["platform"]) if row.get("platform") else JobPlatform.INDEED,
        salary_min=row.get("salary_min"),
        salary_max=row.get("salary_max"),
        fit_score=row.get("fit_score") or 0,
        combined_score=row.get("combined_score") or 0,
    )

    # Use cached DB data — no AI call on brief open
    cached_summary = row.get("tailored_summary") or profile.summary or ""
    cached_skills_raw = row.get("highlighted_skills") or "[]"
    try:
        cached_skills = json.loads(cached_skills_raw) if isinstance(cached_skills_raw, str) else (cached_skills_raw or [])
    except Exception:
        cached_skills = []
    if not cached_skills:
        cached_skills = profile.skills[:12]

    # Key experience highlights — top achievement per role (from profile)
    key_experience = []
    for exp in (profile.experience or [])[:3]:
        achievements = getattr(exp, "achievements", [])
        if achievements:
            key_experience.append({
                "title": exp.title,
                "company": exp.company,
                "highlight": achievements[0],
            })

    # Vault gems — hidden strengths from Obsidian notes
    vault_gems = []
    for g in (getattr(profile, "vault_gems", []) or [])[:4]:
        if isinstance(g, dict):
            vault_gems.append(g.get("insight") or g.get("text") or str(g))
        else:
            vault_gems.append(str(g))

    highlighted_skills = cached_skills
    tailored_summary   = cached_summary
    unique_value_props = (getattr(profile, "unique_value_props", []) or [])[:4]

    return {
        "job_id":            job_id,
        "title":             job.title,
        "company":           job.company,
        "location":          job.location,
        "url":               job.url,
        "fit_score":         round(float(job.fit_score or 0)),
        "combined_score":    round(float(job.combined_score or 0)),
        "match_reasons":     score_breakdown.get("match_reasons", [])[:5],
        "gap_reasons":       score_breakdown.get("gap_reasons", [])[:3],
        "tailored_summary":  tailored_summary,
        "highlighted_skills": highlighted_skills[:10],
        "key_experience":    key_experience,
        "vault_gems":        vault_gems,
        "unique_value_props": unique_value_props,
        "resume_path":       row.get("resume_path") or "",
        "status":            row.get("app_status") or "found",
    }


@app.get("/api/vault/recommendations")
async def get_vault_recommendations():
    """
    Analyze the Obsidian vault + profile and return 8 job title recommendations
    ranked by realistic compensation potential. Takes ~10-15s (one Claude call).
    """
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    profile = agent._load_profile_cache() if not agent.profile else agent.profile
    if not profile:
        raise HTTPException(
            status_code=400,
            detail="No profile found — run Deep Rescan first to build your profile",
        )

    # Try to use the vault index if available; build it on-demand if the path is
    # configured but the index hasn't been loaded yet.  Fall back to profile-only
    # analysis rather than refusing entirely — the recommender handles None gracefully.
    vault_index = agent.vault_index if (agent.vault_index and agent.vault_index._data) else None

    if vault_index is None and config.profile.obsidian_vault_path:
        try:
            from job_agent.parsers.vault_index import VaultIndex
            vi = VaultIndex(
                config.profile.obsidian_vault_path,
                index_dir=config.output.output_dir,
            )
            await asyncio.to_thread(vi.build)
            agent.vault_index = vi
            vault_index = vi
            logger.info("Vault index built on-demand for recommendations")
        except Exception as vi_err:
            logger.warning(f"Could not build vault index on-demand: {vi_err}")
            vault_index = None

    vault_notes = 0
    if vault_index and vault_index._data:
        vault_notes = len(vault_index._data.get("files", []))

    try:
        from job_agent.ai.vault_recommender import VaultRecommender
        recommender = VaultRecommender(config.ai)
        recs = await asyncio.to_thread(recommender.recommend, profile, vault_index)

        # Persist to Obsidian vault so the system accumulates career intelligence over time
        if config.profile.obsidian_vault_path and recs:
            try:
                await asyncio.to_thread(
                    _write_role_scout_to_vault,
                    recs, vault_notes, profile.name, config.profile.obsidian_vault_path,
                )
            except Exception as ve:
                logger.warning(f"[vault] Role Scout note failed: {ve}")

        return {"recommendations": recs, "vault_notes": vault_notes}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"AI response parse error: {e}")
    except Exception as e:
        logger.error(f"Vault recommendations failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs/{job_id}/resume")
async def download_job_resume(job_id: str):
    """
    Download the tailored resume DOCX for a job.
    Serves an existing file instantly; generates on demand if none exists yet (~5-10s).
    """
    if not agent or not tracker:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    rows = tracker.get_jobs(min_score=0, limit=500)
    row = next((r for r in rows if str(r["id"]) == str(job_id)), None)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    # Serve existing file if it's still on disk
    existing_path = row.get("resume_path") or ""
    if existing_path and Path(existing_path).exists():
        filename = Path(existing_path).name
        return FileResponse(
            existing_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=filename,
        )

    # Generate on demand
    profile = agent._load_profile_cache() if not agent.profile else agent.profile
    if not profile:
        raise HTTPException(status_code=400, detail="No profile — run Deep Rescan first")

    job = JobPosting(
        id=job_id,
        title=row.get("title", ""),
        company=row.get("company", ""),
        location=row.get("location") or "",
        description=row.get("description") or "",
        url=row.get("url") or "",
        platform=JobPlatform(row["platform"]) if row.get("platform") else JobPlatform.INDEED,
        salary_min=row.get("salary_min"),
        salary_max=row.get("salary_max"),
        fit_score=float(row.get("fit_score") or 0),
        combined_score=float(row.get("combined_score") or 0),
    )
    try:
        from job_agent.builders.resume_builder import build_resume_docx
        tailored = await asyncio.to_thread(
            agent.tailor.tailor, job, profile, vault_index=agent.vault_index
        )
        resume_path = await asyncio.to_thread(
            build_resume_docx, tailored, config.output.resumes_dir
        )
        tracker.save_resume_path(
            job_id,
            resume_path,
            tailored.tailored_summary or "",
            tailored.highlighted_skills or [],
        )
        filename = Path(resume_path).name
        return FileResponse(
            resume_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=filename,
        )
    except Exception as e:
        logger.error(f"Resume generation failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Resume generation failed: {e}")


@app.post("/api/jobs/{job_id}/generate-resume")
async def generate_resume_json(job_id: str):
    """
    Generate a tailored resume for a job and return JSON status/filename.
    Serves cached result instantly if already on disk.
    Frontend uses this to show a spinner then a named download button.
    """
    if not agent or not tracker:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    rows = tracker.get_jobs(min_score=0, limit=500)
    row = next((r for r in rows if str(r["id"]) == str(job_id)), None)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    # Return cached immediately — no AI cost
    existing_path = row.get("resume_path") or ""
    if existing_path and Path(existing_path).exists():
        return {
            "status": "cached",
            "filename": Path(existing_path).name,
            "resume_url": f"/api/jobs/{job_id}/resume",
        }

    profile = agent._load_profile_cache() if not agent.profile else agent.profile
    if not profile:
        raise HTTPException(status_code=400, detail="No profile — run Deep Rescan first")

    job = JobPosting(
        id=job_id,
        title=row.get("title", ""),
        company=row.get("company", ""),
        location=row.get("location") or "",
        description=row.get("description") or "",
        url=row.get("url") or "",
        platform=JobPlatform(row["platform"]) if row.get("platform") else JobPlatform.INDEED,
        salary_min=row.get("salary_min"),
        salary_max=row.get("salary_max"),
        fit_score=float(row.get("fit_score") or 0),
        combined_score=float(row.get("combined_score") or 0),
    )

    try:
        from job_agent.builders.resume_builder import build_resume_docx
        tailored = await asyncio.to_thread(
            agent.tailor.tailor, job, profile, vault_index=agent.vault_index
        )
        resume_path = await asyncio.to_thread(
            build_resume_docx, tailored, config.output.resumes_dir
        )
        tracker.save_resume_path(
            job_id,
            resume_path,
            tailored.tailored_summary or "",
            tailored.highlighted_skills or [],
        )
        return {
            "status": "generated",
            "filename": Path(resume_path).name,
            "resume_url": f"/api/jobs/{job_id}/resume",
        }
    except Exception as e:
        logger.error(f"generate-resume failed for {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test-run")
async def start_test_run(jobs_per_platform: int = 3, min_score: float = 40.0):
    """
    Test run: apply to up to `jobs_per_platform` jobs on each platform the user
    is currently logged into.  Uses a lower min_score so there are enough candidates.
    Results stream via WebSocket /ws/logs exactly like a normal auto-apply run.
    """
    global pipeline_running, current_step
    if pipeline_running:
        raise HTTPException(status_code=409, detail="Pipeline already running")
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    # Check which platforms have active sessions
    logged_in = await asyncio.get_event_loop().run_in_executor(None, _check_login_sync)
    active_platforms = [p for p, ok in logged_in.items() if ok]
    if not active_platforms:
        raise HTTPException(status_code=400, detail="Not logged into any platform — log in first via Config → Platform Logins")

    # Pull jobs from DB; pick up to jobs_per_platform from each active platform
    _cfg_now = load_config()
    _manual_only = set(x.lower() for x in _cfg_now.automation.manual_only_platforms)
    all_rows = tracker.get_jobs(min_score=min_score, limit=500)
    selected = []
    platform_counts: dict = {}
    for row in all_rows:
        p = (row.get("platform") or "").lower()
        if p not in active_platforms:
            continue
        if p in _manual_only:
            continue
        if platform_counts.get(p, 0) >= jobs_per_platform:
            continue
        if row.get("app_status") in ("applied", "failed", "needs_manual"):
            continue
        if tracker.already_applied(str(row["id"])):
            continue
        if tracker.failure_count(str(row["id"])) >= 2:
            continue  # failed 2+ times already — skip
        selected.append(row)
        platform_counts[p] = platform_counts.get(p, 0) + 1

    if not selected:
        raise HTTPException(
            status_code=404,
            detail=f"No eligible jobs found on logged-in platforms ({', '.join(active_platforms)}). Run a Rescan first.",
        )

    total = sum(platform_counts.values())
    await manager.broadcast(json.dumps({"type": "log", "message": f"[TEST RUN] {total} jobs selected across {list(platform_counts)} — starting",
                                         "timestamp": datetime.now().isoformat()}))

    async def _run():
        import uuid as _uuid
        from job_agent.models import Application, ApplicationStatus, JobPosting, JobPlatform, TailoredResume
        from job_agent.builders.resume_builder import build_resume_docx
        from job_agent.automation.ats_handlers import detect_ats as _detect_ats
        global pipeline_running, current_step
        pipeline_running = True
        current_step = "test-run"

        cfg = load_config()
        global _run_log
        if _run_log is None:
            _run_log = RunLog(str(Path(cfg.output.output_dir) / "run_log.md"))

        async def log(msg: str, type_: str = "log"):
            await manager.broadcast(json.dumps({
                "type": type_, "message": msg,
                "timestamp": datetime.now().isoformat(),
            }))

        try:
            profile = agent.profile or agent._load_profile_cache()
            if not profile:
                await log("[TEST] No profile found — run Deep Rescan on the Profile tab first.", "error")
                return

            _run_log.start_run(f"[TEST] {total} jobs ({', '.join(f'{p}:{n}' for p, n in platform_counts.items())})")
            applied_count = 0
            needs_manual_count = 0
            failed_count = 0

            for i, row in enumerate(selected):
                job_id = str(row["id"])
                title = row.get("title", "")
                company = row.get("company", "")
                current_step = f"[TEST] Applying: {company} ({i+1}/{total})"
                await log(f"[TEST] [{i+1}/{total}] {title} @ {company}")

                job = JobPosting(
                    id=job_id,
                    title=title,
                    company=company,
                    location=row.get("location") or "",
                    description=row.get("description") or "",
                    url=row.get("url") or "",
                    platform=JobPlatform(row["platform"]) if row.get("platform") else JobPlatform.INDEED,
                    salary_min=row.get("salary_min"),
                    salary_max=row.get("salary_max"),
                    fit_score=row.get("fit_score") or 0,
                    combined_score=row.get("combined_score") or 0,
                )

                try:
                    tailored = await asyncio.wait_for(
                        asyncio.to_thread(agent.tailor.tailor, job, profile, vault_index=agent.vault_index),
                        timeout=60.0,
                    )
                except Exception as te:
                    await log(f"[TEST] Resume tailor failed for {company}: {te}", "error")
                    tailored = TailoredResume(
                        job=job, profile=profile,
                        tailored_summary=profile.summary,
                        highlighted_skills=profile.skills[:10],
                    )

                try:
                    await asyncio.to_thread(build_resume_docx, tailored, config.output.resumes_dir)
                except Exception:
                    pass

                app_obj = Application(
                    id=str(_uuid.uuid4()),
                    job=job,
                    resume=tailored,
                    status=ApplicationStatus.QUEUED,
                )
                app_id = tracker.create_application(app_obj)
                app_obj.id = app_id

                try:
                    current_step = f"[TEST] Submitting: {company}"
                    result = await asyncio.wait_for(
                        asyncio.to_thread(agent.agent.apply_one, app_obj),
                        timeout=600.0,
                    )
                    tracker.sync_application(result)

                    if result.status == ApplicationStatus.APPLIED:
                        applied_count += 1
                        await log(f"[TEST] ✓ APPLIED — {title} @ {company}", "complete")
                        await manager.broadcast(json.dumps({"type": "apply_update", "job_id": job_id,
                            "status": "applied", "company": company, "title": title,
                            "notes": result.notes or "", "timestamp": datetime.now().isoformat()}))
                    elif result.status == ApplicationStatus.NEEDS_MANUAL:
                        needs_manual_count += 1
                        await log(f"[TEST] ⚠ NEEDS MANUAL — {company}: {result.error}")
                        await manager.broadcast(json.dumps({"type": "apply_update", "job_id": job_id,
                            "status": "needs_manual", "company": company, "title": title,
                            "error": result.error, "timestamp": datetime.now().isoformat()}))
                    else:
                        failed_count += 1
                        await log(f"[TEST] ✗ FAILED — {company}: {result.error}", "error")
                        await manager.broadcast(json.dumps({"type": "apply_update", "job_id": job_id,
                            "status": "failed", "company": company, "title": title,
                            "error": result.error, "timestamp": datetime.now().isoformat()}))

                    _run_log.log_result(
                        title=title, company=company, url=row.get("url", ""),
                        status=result.status.value, ats=_detect_ats(row.get("url", "")),
                        error=result.error, notes=result.notes,
                        fields_filled=len(result.form_data) if result.form_data else 0,
                    )

                except asyncio.TimeoutError:
                    needs_manual_count += 1
                    err_msg = "Apply timed out (10 min cap) — apply manually"
                    tracker.update_application(app_obj.id, status="needs_manual", error=err_msg)
                    await log(f"[TEST] ⚠ TIMEOUT — {company}: exceeded 10 min", "error")
                    _run_log.log_result(title=title, company=company,
                        url=row.get("url", ""), status="needs_manual", error=err_msg)
                except Exception as e:
                    failed_count += 1
                    logger.error(f"[TEST] Apply exception for {company}: {e}")
                    tracker.update_application(app_obj.id, status="failed", error=str(e))
                    await log(f"[TEST] ✗ ERROR — {company}: {e}", "error")
                    _run_log.log_result(title=title, company=company,
                        url=row.get("url", ""), status="failed", error=str(e))

            _run_log.finish_run()
            await log(f"[TEST] Done — {applied_count} applied, {needs_manual_count} needs manual, {failed_count} failed", "complete")

        except Exception as e:
            logger.error(f"Test run failed: {e}")
            await log(f"[TEST] FAILED: {e}", "error")
        finally:
            pipeline_running = False
            current_step = ""

    asyncio.create_task(_run())
    breakdown = ", ".join(f"{p}:{n}" for p, n in platform_counts.items())
    return {"ok": True, "jobs": total, "platforms": breakdown,
            "message": f"Test run started — {total} jobs ({breakdown}). Watch the log stream."}


@app.post("/api/auto-apply")
async def start_auto_apply(min_score: float = 70.0, max_apply: int = 10):
    """Start the auto-apply pipeline for scored jobs above min_score."""
    global pipeline_running, current_step
    if pipeline_running:
        raise HTTPException(status_code=409, detail="Pipeline already running")
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    pipeline_running = True
    current_step = "Auto-Apply: Starting..."
    asyncio.create_task(run_auto_apply_background(min_score, max_apply))
    return {"status": "started", "min_score": min_score, "max_apply": max_apply}


@app.post("/api/jobs/{job_id}/apply")
async def apply_one_job(job_id: str):
    """
    Apply to a single job by ID — triggered from the job card Apply button.
    Runs the full tailor + apply pipeline for just this one job.
    Returns result immediately (runs synchronously in a thread so the caller waits).
    """
    global pipeline_running, current_step, _run_log
    if pipeline_running:
        raise HTTPException(status_code=409, detail="Pipeline already running — wait for it to finish")
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    if not tracker:
        raise HTTPException(status_code=503, detail="Tracker not ready")

    if _run_log is None:
        cfg = load_config()
        _run_log = RunLog(str(Path(cfg.output.output_dir) / "run_log.md"))

    # Load job row
    rows = tracker.get_jobs(min_score=0, limit=500)
    row = next((r for r in rows if str(r["id"]) == str(job_id)), None)
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Only block if definitively applied — needs_manual means automation failed, retry is allowed
    existing = tracker.get_application_by_job_id(job_id)
    if existing and existing.get("status") in ("applied", "interview", "offer"):
        raise HTTPException(status_code=409, detail="Already applied to this job")

    # Certain platforms (Indeed, LinkedIn Easy Apply) use bot detection / OAuth walls
    # that Playwright cannot reliably beat. Route them to needs_manual immediately.
    job_platform = str(row.get("platform", "")).lower()
    cfg_now = load_config()
    manual_only = set(x.lower() for x in (cfg_now.automation.manual_only_platforms or []))
    manual_only.update({"indeed", "linkedin"})  # always manual regardless of config
    if job_platform in manual_only:
        import uuid as _uuid2
        from job_agent.models import Application as _App, ApplicationStatus as _AS, JobPosting as _JP2, JobPlatform as _JPF2
        _job_tmp = _JP2(
            id=job_id, title=row.get("title",""), company=row.get("company",""),
            location=row.get("location",""), description="", url=row.get("url",""),
            platform=_JPF2(row["platform"]) if row.get("platform") else _JPF2.INDEED,
        )
        _app_tmp = _App(
            id=str(_uuid2.uuid4()), job=_job_tmp, resume=None,
            status=_AS.NEEDS_MANUAL,
            error="Indeed blocks automation — use the Apply Kit to apply manually.",
            notes="Platform blocked: indeed",
        )
        tracker.create_application(_app_tmp)
        return {
            "status": "needs_manual",
            "title": row.get("title", ""),
            "company": row.get("company", ""),
            "error": "Indeed blocks automation — open the Apply Kit to apply manually with your tailored resume.",
        }

    profile = agent.profile
    if not profile:
        profile = agent._load_profile_cache()
    if not profile:
        raise HTTPException(status_code=503, detail="No profile — run Deep Rescan on the Profile tab first")

    pipeline_running = True
    current_step = f"Applying: {row.get('company', '')}"

    try:
        title   = row.get("title", "")
        company = row.get("company", "")
        job = JobPosting(
            id=job_id,
            title=title,
            company=company,
            location=row.get("location") or "",
            description=row.get("description") or "",
            url=row.get("url") or "",
            platform=JobPlatform(row["platform"]) if row.get("platform") else JobPlatform.INDEED,
            salary_min=row.get("salary_min"),
            salary_max=row.get("salary_max"),
            fit_score=row.get("fit_score") or 0,
            combined_score=row.get("combined_score") or 0,
        )

        import uuid as _uuid
        from job_agent.models import Application, ApplicationStatus, LazyResume
        from job_agent.automation.ats_handlers import detect_ats as _detect_ats

        # Lazy resume — AI generation deferred until agent hits a file/cover-letter field
        lazy = LazyResume(
            job=job, profile=profile, tailor=agent.tailor,
            resumes_dir=config.output.resumes_dir,
            auto_cover_letter=config.automation.auto_cover_letter,
            vault_index=agent.vault_index,
        )

        # Create application record
        app_obj = Application(
            id=str(_uuid.uuid4()),
            job=job,
            resume=lazy,
            status=ApplicationStatus.QUEUED,
        )
        app_id = tracker.create_application(app_obj)
        app_obj.id = app_id

        # Run the apply
        result = await asyncio.to_thread(agent.agent.apply_one, app_obj)
        tracker.sync_application(result)

        # Write vault note only if the resume was actually generated during apply
        if config.profile.obsidian_vault_path and lazy.tailored_summary:
            try:
                await asyncio.to_thread(
                    _write_resume_to_vault, lazy, config.profile.obsidian_vault_path
                )
            except Exception as _ve:
                logger.warning(f"[vault] resume note (single apply): {_ve}")

        # Log to run log
        _run_log.log_result(
            title=title, company=company, url=row.get("url", ""),
            status=result.status.value, ats=_detect_ats(row.get("url", "")),
            error=result.error, notes=result.notes,
            fields_filled=len(result.form_data) if result.form_data else 0,
        )

        # Broadcast WS update so the card refreshes
        msg = json.dumps({
            "type": "apply_update",
            "job_id": job_id,
            "status": result.status.value,
            "company": company,
            "title": title,
            "error": result.error or "",
            "notes": result.notes or "",
        })
        await manager.broadcast(msg)

        return {
            "job_id": job_id,
            "status": result.status.value,
            "error": result.error,
            "notes": result.notes,
            "fields_filled": len(result.form_data) if result.form_data else 0,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"apply-one {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        pipeline_running = False
        current_step = None


@app.get("/api/needs-manual")
async def get_needs_manual():
    """Return all jobs that failed automation and need a manual application."""
    if not tracker:
        return []
    try:
        return tracker.get_needs_manual()
    except Exception as e:
        logger.error(f"needs-manual fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/jobs/{job_id}/mark-applied-manually")
async def mark_applied_manually(job_id: str):
    """User confirms they applied to this job by hand."""
    if not tracker:
        raise HTTPException(status_code=503, detail="Tracker not ready")
    try:
        tracker.mark_applied_manually(job_id)
        await manager.broadcast(json.dumps({
            "type": "apply_update",
            "job_id": job_id,
            "status": "applied",
            "timestamp": datetime.now().isoformat(),
        }))
        return {"job_id": job_id, "status": "applied"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/jobs/{job_id}/dismiss")
async def dismiss_job(job_id: str):
    """Remove a job from the list — user chose not to apply."""
    if not tracker:
        raise HTTPException(status_code=503, detail="Tracker not ready")
    try:
        tracker.dismiss_job(job_id)
        return {"job_id": job_id, "status": "dismissed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/jobs/{job_id}/status")
async def update_job_status(job_id: str, status: str):
    """Manually update the application status for a job."""
    valid = {"found", "applied", "interview", "offer", "rejected", "withdrawn", "needs_manual", "dismissed"}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {sorted(valid)}")

    try:
        db_path = Path(config.output.output_dir) / "applications.db"
        conn = sqlite3.connect(str(db_path))
        now = datetime.now().isoformat()

        existing = conn.execute(
            "SELECT id FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE applications SET status=?, updated_at=? WHERE job_id=?",
                (status, now, job_id),
            )
        else:
            import uuid as _uuid
            conn.execute(
                """INSERT INTO applications (id, job_id, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(_uuid.uuid4()), job_id, status, now, now),
            )

        conn.commit()
        conn.close()
        return {"job_id": job_id, "status": status}
    except Exception as e:
        logger.error(f"Status update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Smart Fill (Browser Extension) ──

@app.post("/api/smart-fill")
async def smart_fill(body: dict):
    """
    Extension sends form fields from any job site.
    Returns classified + filled values using global semantic learning.
    Body: {domain, url, fields: [{id, label, name, type, placeholder, options, required}],
           job_context: {title, company, url}}
    """
    if not agent or not smart_filler:
        raise HTTPException(status_code=503, detail="Agent not ready")

    domain      = body.get("domain", "")
    fields      = body.get("fields", [])
    job_context = body.get("job_context")

    if not fields:
        return {"fills": [], "instant_hits": 0, "ai_classifier_calls": 0}

    profile = agent._load_profile_cache() if not agent.profile else agent.profile
    if not profile:
        raise HTTPException(status_code=400, detail="No profile found — run Deep Rescan first")

    fills, meta = await asyncio.to_thread(
        smart_filler.fill_fields, fields, profile, job_context, domain
    )

    return {
        "fills":               fills,
        "instant_hits":        meta["instant_hits"],
        "ai_classifier_calls": meta["ai_classifier_calls"],
        "ai_answer_calls":     meta["ai_answer_calls"],
        "answer_cache_hits":   meta["answer_cache_hits"],
        "total_fields":        len(fields),
        "domain":              domain,
    }


@app.post("/api/learn-pattern")
async def learn_pattern(body: dict):
    """
    Extension calls this after a successful form submission.
    Reinforces global field semantics and records the submission.
    Body: {domain, url,
           confirmed_fills: [{fingerprint, canonical_type, label, name}],
           corrections: [{fingerprint, wrong_type, correct_type, label, name}]}
    """
    if not semantics_db:
        raise HTTPException(status_code=503, detail="Semantics DB not ready")

    domain = body.get("domain", "")
    url    = body.get("url", "")

    # Confirmed fills — reinforce the mapping
    for fill in body.get("confirmed_fills", []):
        semantics_db.record_semantic(
            fill["fingerprint"], fill["canonical_type"],
            fill.get("label", ""), fill.get("name", ""), domain
        )

    # Corrections — user changed a value, so the old mapping was wrong
    for corr in body.get("corrections", []):
        semantics_db.record_correction(
            corr["fingerprint"], corr["correct_type"],
            corr.get("label", ""), corr.get("name", ""), domain
        )

    import uuid as _uuid
    semantics_db.log_submission(
        str(_uuid.uuid4()), domain, url,
        fields_filled=body.get("fields_filled", 0),
        instant_hits=body.get("instant_hits", 0),
        ai_calls=body.get("ai_calls", 0),
    )

    return {
        "ok": True,
        "reinforced": len(body.get("confirmed_fills", [])),
        "corrected":  len(body.get("corrections", [])),
        "domain": domain,
    }


@app.post("/api/learn-field")
async def learn_field(body: dict):
    """
    Called by filler.js when the user manually fills or corrects a field.
    Saves to FieldSemanticsDB (field knowledge) and answer_cache (question answers),
    then writes a human-readable Obsidian vault note so the learning is visible and
    searchable, and feeds back into the profile on the next run.

    Body: {
      fields: [{fingerprint, label, name, type, value, canonical_type,
                was_auto_filled, was_corrected, domain}],
      url: str,
      domain: str,
      job_context: {title, company, url}
    }
    """
    if not semantics_db:
        raise HTTPException(status_code=503, detail="Semantics DB not ready")

    fields      = body.get("fields", [])
    domain      = body.get("domain", "")
    url         = body.get("url", "")
    job_context = body.get("job_context") or {}
    company     = job_context.get("company", "")
    job_title   = job_context.get("title", "")

    learned, corrected_count, question_count = 0, 0, 0

    for f in fields:
        fp         = f.get("fingerprint", "")
        label      = f.get("label", "")
        name       = f.get("name", "")
        value      = (f.get("value") or "").strip()
        canon      = f.get("canonical_type") or ""
        corrected  = f.get("was_corrected", False)
        auto_filled = f.get("was_auto_filled", False)

        if not fp or not value:
            continue

        # Record the corrected mapping so we get it right next time
        if corrected and canon:
            semantics_db.record_correction(fp, canon, label, name, domain)
            corrected_count += 1
        elif not auto_filled:
            # User filled something we missed — learn it
            if canon:
                semantics_db.record_semantic(fp, canon, label, name, domain)
            learned += 1

        # Cache open-ended question answers (question.* types)
        if canon and canon.startswith("question.") and len(value) > 15:
            semantics_db.cache_answer(canon, company, job_title, value)
            question_count += 1

    # Write to Obsidian vault for human visibility + profile feed-back
    vault_path = None
    try:
        if config and config.profile.obsidian_vault_path:
            vault_path = await asyncio.to_thread(
                _write_learned_answers_to_vault,
                config.profile.obsidian_vault_path,
                fields, job_context, domain,
            )
    except Exception as e:
        logger.warning(f"learn-field vault write failed: {e}")

    return {
        "ok": True,
        "learned": learned,
        "corrected": corrected_count,
        "question_answers_cached": question_count,
        "vault_note": vault_path,
    }


def _write_learned_answers_to_vault(
    vault_path: str,
    fields: list,
    job_context: dict,
    domain: str,
) -> str:
    """
    Append learned field values to a single Obsidian note:
    {vault}/Job Agent - Learned Answers.md

    The note accumulates all manual fills / corrections over time.
    VaultIndex picks it up on the next profile rebuild and bakes it into
    the profile so future applications are pre-filled automatically.
    """
    from datetime import datetime as _dt
    vault = Path(vault_path)
    if not vault.exists():
        return ""

    note_path = vault / "Job Agent - Learned Answers.md"
    now       = _dt.now().strftime("%Y-%m-%d %H:%M")
    company   = job_context.get("company", domain)
    job_title = job_context.get("title", "")

    # Filter to fields worth saving (skip short/trivial values)
    saveable = [
        f for f in fields
        if (f.get("value") or "").strip() and len((f.get("value") or "").strip()) > 2
        and f.get("label") or f.get("canonical_type")
    ]
    if not saveable:
        return ""

    # Build the new section
    header = f"\n\n## {company}" + (f" — {job_title}" if job_title else "") + f"\n*{now} · {domain}*\n"
    rows = []
    for f in saveable:
        label = f.get("label") or f.get("canonical_type") or f.get("name") or "Field"
        value = (f.get("value") or "").strip()
        tag   = " *(corrected)*" if f.get("was_corrected") else ""
        if "\n" in value or len(value) > 80:
            rows.append(f"\n**{label}**{tag}\n> {value.replace(chr(10), chr(10)+'> ')}\n")
        else:
            rows.append(f"- **{label}**{tag}: {value}")

    new_content = header + "\n".join(rows)

    # Ensure the note exists with a header
    if not note_path.exists():
        note_path.write_text(
            "# Job Agent — Learned Answers\n\n"
            "This note is auto-updated whenever you manually fill or correct a field "
            "on a job application. The agent reads it back during profile rebuilds "
            "to improve future auto-fills.\n",
            encoding="utf-8",
        )

    with open(note_path, "a", encoding="utf-8") as f:
        f.write(new_content)

    return str(note_path)


@app.get("/api/known-sites")
async def known_sites():
    """List all domains where the extension has submitted applications."""
    if not semantics_db:
        return []
    return semantics_db.list_known_domains()


@app.get("/api/field-intelligence")
async def field_intelligence():
    """
    Returns global field semantics stats — how much the system has learned.
    """
    if not semantics_db:
        return {}
    return semantics_db.get_stats()


@app.get("/api/site-pattern/{domain:path}")
async def site_pattern(domain: str):
    """Field intelligence stats for a specific domain (submission history)."""
    if not semantics_db:
        return {}
    return {"domain": domain, "stats": semantics_db.get_stats()}


@app.get("/api/improvements")
async def get_improvements():
    """
    Self-improvement log: open issues, recent wins, weekly stats.
    Used by the UI and written to the Obsidian vault after each run.
    """
    try:
        from job_agent.db.improvement_tracker import ImprovementTracker
        db_path = config.output.db_path if config else "./output/applications.db"
        itracker = ImprovementTracker(db_path)
        return {
            "open_issues":  itracker.get_open_improvements(),
            "recent_wins":  itracker.get_recent_successes(days=7),
            "weekly_stats": itracker.get_weekly_stats(),
        }
    except Exception as e:
        logger.error(f"improvements: {e}")
        return {"open_issues": [], "recent_wins": [], "weekly_stats": {}}


# ── Config Read / Write ──

CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"
BROWSER_PROFILE_DIR = Path.home() / ".job_agent" / "browser_profile"  # short path avoids Windows MAX_PATH

# Serialises all access to the Playwright persistent profile directory.
# Chrome can only have one process per profile dir at a time — without this,
# get_login_status and open_login race each other and the second one silently fails.
_playwright_lock = threading.Lock()

@app.get("/api/config")
async def get_config():
    """Return the current config.yaml as JSON."""
    try:
        import yaml
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config")
async def save_config(body: dict):
    """Write updated config back to config.yaml."""
    try:
        import yaml
        # Read existing file to preserve comments structure — merge at top level
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
        # Deep-merge: only update keys present in body
        for section, value in body.items():
            if isinstance(value, dict) and isinstance(existing.get(section), dict):
                existing[section].update(value)
            else:
                existing[section] = value
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        # Reload config in memory and propagate live references so keyword/platform
        # changes take effect immediately without a server restart
        global config, agent
        config = load_config(str(CONFIG_PATH))
        if agent:
            agent.config = config
            # searcher.config IS the SearchConfig — update so new keywords/locations/platforms are used
            agent.searcher.config = config.search
            # scorer and tailor store api_key+model directly, not a config object
            if hasattr(agent, 'scorer') and agent.scorer and config.ai.anthropic_api_key:
                import anthropic as _ant
                agent.scorer.client = _ant.Anthropic(api_key=config.ai.anthropic_api_key)
                agent.scorer.model = config.ai.scoring_model
            if hasattr(agent, 'tailor') and agent.tailor and config.ai.anthropic_api_key:
                import anthropic as _ant
                agent.tailor.client = _ant.Anthropic(api_key=config.ai.anthropic_api_key)
                agent.tailor.model = config.ai.resume_model
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── WebSocket for Real-time Logs ──

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        async with self._lock:
            connections = self.active_connections[:]
        dead = []
        for connection in connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.warning(f"Failed to send WebSocket message: {e}")
                dead.append(connection)
        if dead:
            async with self._lock:
                for connection in dead:
                    if connection in self.active_connections:
                        self.active_connections.remove(connection)

manager = ConnectionManager()


def _captcha_notify_fn(info: dict):
    """
    Called synchronously from the ApplicationAgent thread when a CAPTCHA is detected
    or auto-resolved. Schedules a WebSocket broadcast on the main asyncio event loop.
    """
    global _captcha_job_info
    if info.get("type") == "captcha_resolved_auto":
        # Agent detected the CAPTCHA was solved without the button being clicked
        _captcha_job_info = {}
        _captcha_event.set()
        if _main_loop and not _main_loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast(json.dumps({
                        "type": "captcha_resolved",
                        "timestamp": datetime.now().isoformat(),
                    })),
                    _main_loop,
                )
            except RuntimeError:
                pass
        return
    _captcha_job_info = info
    if _main_loop and not _main_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps({
                    **info,
                    "timestamp": datetime.now().isoformat(),
                })),
                _main_loop,
            )
        except RuntimeError:
            pass  # Event loop shutting down — safe to swallow


@app.post("/api/captcha-solved")
async def captcha_solved():
    """
    Called by the frontend when the user has manually solved the CAPTCHA.
    Sets the flag on the agent AND the threading.Event so the paused thread resumes.
    """
    if agent and hasattr(agent, "agent") and hasattr(agent.agent, "_captcha_solved"):
        agent.agent._captcha_solved = True
    _captcha_event.set()
    await manager.broadcast(json.dumps({
        "type": "captcha_resolved",
        "timestamp": datetime.now().isoformat(),
    }))
    return {"ok": True, "message": "Resuming auto-apply..."}


@app.get("/api/field-memory")
async def get_field_memory_list():
    return {"memories": tracker.get_all_memories()}


@app.post("/api/field-memory")
async def save_field_memory_entry(body: dict):
    label = (body.get("label") or "").strip()
    answer = (body.get("answer") or "").strip()
    context = (body.get("context") or "").strip()
    if not label or not answer:
        raise HTTPException(status_code=400, detail="label and answer are required")
    mem_id = tracker.save_field_memory(label, answer, context)
    # Sync memory_lookup_fn after adding so the running agent picks it up
    if agent and hasattr(agent, "agent"):
        agent.agent.memory_lookup_fn = tracker.get_field_memory
    xp = 75
    memories = tracker.get_all_memories()
    achievements = []
    if len(memories) >= 1: achievements.append("co_pilot")
    if len(memories) >= 10: achievements.append("ai_trainer")
    if len(memories) >= 50: achievements.append("mind_meld")
    await manager.broadcast(json.dumps({
        "type": "xp_gain",
        "amount": xp,
        "reason": "field_taught",
        "achievements": achievements,
        "timestamp": datetime.now().isoformat(),
    }))
    return {"ok": True, "id": mem_id, "xp_earned": xp}


@app.delete("/api/field-memory/{memory_id}")
async def delete_field_memory(memory_id: str):
    tracker.delete_memory(memory_id)
    return {"ok": True}


@app.get("/api/captcha-status")
async def captcha_status():
    """Returns whether the bot is currently paused waiting for a CAPTCHA solve."""
    return {
        "waiting": not _captcha_event.is_set(),
        "job_info": _captcha_job_info,
    }


@app.post("/api/jobs/{job_id}/assist")
async def log_assist(job_id: str, body: AssistLog):
    """Record what the user did to resolve a stuck application and award XP."""
    global _assist_needed_info
    from job_agent.db.improvement_tracker import ImprovementTracker, _classify_error
    db_path = config.output.db_path if config else "./output/applications.db"
    imp = ImprovementTracker(db_path)
    info = _assist_needed_info if _assist_needed_info.get("job_id") == job_id else {}
    result = imp.log_user_assist(
        job_id=job_id,
        ats=info.get("ats", "generic"),
        url=info.get("url", ""),
        company=info.get("company", ""),
        job_title=info.get("job_title", ""),
        error_type=info.get("error_type", ""),
        error_msg=info.get("error", ""),
        action_taken=body.action_taken,
        action_detail=body.action_detail,
    )
    total = result["total_assists"]
    achievements = []
    if total == 1:
        achievements.append("co_pilot")
    if total == 10:
        achievements.append("ai_trainer")
    if total == 50:
        achievements.append("mind_meld")
    await manager.broadcast(json.dumps({
        "type": "xp_gain",
        "amount": result["xp_awarded"],
        "label": "Human Override",
        "achievements": achievements,
        "timestamp": datetime.now().isoformat(),
    }))
    return {"xp_awarded": result["xp_awarded"], "total_assists": total, "achievements": achievements}


@app.get("/api/assist-hints")
async def get_assist_hints(ats: str = "", error_type: str = ""):
    """Return historical intervention hints for a given ATS + error type."""
    from job_agent.db.improvement_tracker import ImprovementTracker
    db_path = config.output.db_path if config else "./output/applications.db"
    imp = ImprovementTracker(db_path)
    return imp.get_assist_hints(ats, error_type)


@app.get("/api/performance")
async def get_performance():
    """
    System intelligence endpoint.
    Returns run trend history + improvement ROI list for the System Intel panel.
    """
    from job_agent.db.improvement_tracker import ImprovementTracker
    db_path = config.output.db_path if config else "./output/applications.db"
    runs = tracker.get_run_trends(limit=10) if tracker else []
    imp = ImprovementTracker(db_path)
    roi = imp.get_improvement_roi(limit=6)
    summary = tracker.get_score_summary() if tracker else {}
    return {
        "runs": runs,
        "roi":  roi,
        "best_score":   summary.get("best_score", 0),
        "last_score":   summary.get("last_score", 0),
        "streak":       summary.get("streak", 0),
        "success_rate": summary.get("success_rate", 0),
    }


@app.post("/api/open-browser")
async def open_browser(request: Request):
    """
    Open the persistent browser profile to a URL so the user can sign in.
    Used for Google account setup, Indeed login, LinkedIn login, etc.
    The browser stays open — user signs in, then closes it; credentials are saved.
    """
    data = await request.json()
    url = data.get("url", "https://accounts.google.com")
    BROWSER_PROFILE_DIR = Path.home() / ".job_agent" / "browser_profile"
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import asyncio as _asyncio
        from playwright.async_api import async_playwright
        async def _open():
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    str(BROWSER_PROFILE_DIR),
                    headless=False,
                    args=[
                        "--start-maximized",
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                    ignore_default_args=["--enable-automation"],
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = await ctx.new_page()
                await page.goto(url)
                await page.wait_for_event("close", timeout=0)  # wait until tab closed
                await ctx.close()
        _asyncio.create_task(_open())
        return {"ok": True, "message": f"Browser opening {url} — sign in and close the tab when done."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Platform Login Status ──────────────────────────────────────────────────────

_LOGIN_URLS = {
    "linkedin":     "https://www.linkedin.com/feed/",
    "indeed":       "https://www.indeed.com/",
    "glassdoor":    "https://www.glassdoor.com/",
    "ziprecruiter": "https://www.ziprecruiter.com/",
}

_LOGIN_CHECK_URLS = {
    "linkedin":     "https://www.linkedin.com/feed/",
    "indeed":       "https://www.indeed.com/account/login",
    "glassdoor":    "https://www.glassdoor.com/profile/login_input.htm",
    "ziprecruiter": "https://www.ziprecruiter.com/login",
}

_LOGIN_OPEN_URLS = {
    "linkedin":     "https://www.linkedin.com/login",
    "indeed":       "https://secure.indeed.com/account/login",
    "glassdoor":    "https://www.glassdoor.com/profile/login_input.htm",
    "ziprecruiter": "https://www.ziprecruiter.com/login",
}


def _check_login_sync() -> dict:
    """
    Check login state by reading the Chromium cookie DB directly — no browser launch.
    Launching headless Chromium against the persistent profile lets sites detect
    automation and invalidate saved session cookies.

    Auth cookie keys:
      LinkedIn:     li_at
      Indeed:       __Secure-PassportAuthProxy-BearerToken | CTK | INDEED_CSRF_TOKEN | IL
      Glassdoor:    GSID | gdId | gdsid
      ZipRecruiter: ZIPRECRUITERSESSION | jobseeker_token | rememberedUserEmail
    """
    result = {"linkedin": False, "indeed": False, "glassdoor": False, "ziprecruiter": False}

    # Chrome 112+ stores cookies at Default/Network/Cookies (not Default/Cookies)
    cookie_db = BROWSER_PROFILE_DIR / "Default" / "Network" / "Cookies"
    if not cookie_db.exists():
        cookie_db = BROWSER_PROFILE_DIR / "Default" / "Cookies"  # fallback for older builds
    if not cookie_db.exists():
        # Profile never used — not logged in anywhere
        return result

    try:
        import shutil, tempfile
        # Copy to a temp file so we don't lock the live Chromium profile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(str(cookie_db), tmp_path)

        conn = sqlite3.connect(tmp_path)
        try:
            rows = conn.execute(
                "SELECT host_key, name FROM cookies WHERE "
                "host_key LIKE '%linkedin.com%' OR host_key LIKE '%indeed.com%' "
                "OR host_key LIKE '%glassdoor.com%' OR host_key LIKE '%ziprecruiter.com%'"
            ).fetchall()
        finally:
            conn.close()

        import os
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        linkedin_cookies     = {name for host, name in rows if "linkedin.com"     in host}
        indeed_cookies       = {name for host, name in rows if "indeed.com"       in host}
        glassdoor_cookies    = {name for host, name in rows if "glassdoor.com"    in host}
        ziprecruiter_cookies = {name for host, name in rows if "ziprecruiter.com" in host}

        result["linkedin"] = "li_at" in linkedin_cookies
        result["indeed"] = bool(indeed_cookies & {
            "__Secure-PassportAuthProxy-BearerToken", "CTK", "INDEED_CSRF_TOKEN", "IL"
        })
        result["glassdoor"] = bool(glassdoor_cookies & {"GSID", "gdId", "gdsid"})
        result["ziprecruiter"] = bool(ziprecruiter_cookies & {
            "ZIPRECRUITERSESSION", "jobseeker_token", "rememberedUserEmail"
        })

    except Exception as e:
        logger.error(f"Login status check failed: {e}")

    return result


@app.get("/api/login-status")
async def get_login_status():
    """Check login state for each platform (runs in thread, sync Playwright)."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _check_login_sync)
    return result


_STEALTH_ARGS = [
    "--start-maximized",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-extensions-except=",
    "--disable-plugins-discovery",
]

# Minimal args for real Chrome (channel="chrome") — don't over-flag it
_STEALTH_ARGS_REAL_CHROME = [
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]
_STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_STEALTH_SCRIPT = """
    (() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const p = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                ];
                p.__proto__ = PluginArray.prototype;
                return p;
            }
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        if (!window.chrome) {
            window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
        }
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
        const origQuery = window.navigator.permissions ? window.navigator.permissions.query.bind(window.navigator.permissions) : null;
        if (origQuery) {
            window.navigator.permissions.query = (params) =>
                params.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(params);
        }
    })();
"""


def _open_login_sync(platform: str):
    """
    Opens a visible browser window so the user can log in and save the session.

    ZipRecruiter (and Glassdoor) use DataDome / Cloudflare Turnstile which
    fingerprints the browser binary itself — canvas, TLS stack, HTTP/2, audio.
    Playwright's bundled Chromium fails these checks even with stealth args.
    For those platforms we launch the user's real installed Chrome via
    channel="chrome", which passes bot detection transparently.  Cookies still
    land in BROWSER_PROFILE_DIR so the apply pipeline can use them.

    Falls back to bundled Chromium if Chrome is not installed.
    """
    from playwright.sync_api import sync_playwright

    # Platforms that need real Chrome to pass bot/CAPTCHA checks
    _NEEDS_REAL_CHROME = {"ziprecruiter", "glassdoor"}

    # Home page warmup before hitting the login URL directly
    _WARMUP_URLS = {
        "ziprecruiter": "https://www.ziprecruiter.com",
        "glassdoor":    "https://www.glassdoor.com",
    }

    def _is_logged_in(url: str) -> bool:
        lurl = url.lower()
        if platform == "ziprecruiter":
            return (
                "ziprecruiter.com" in lurl
                and "login" not in lurl
                and "sign-in" not in lurl
                and "challenges.cloudflare.com" not in lurl
            )
        if platform == "glassdoor":
            return (
                "glassdoor.com" in lurl
                and "login" not in lurl
                and "signin" not in lurl
            )
        return "login" not in lurl and "checkpoint" not in lurl and "signin" not in lurl

    def _launch(pw, use_real_chrome: bool):
        if use_real_chrome:
            return pw.chromium.launch_persistent_context(
                str(BROWSER_PROFILE_DIR),
                channel="chrome",           # user's actual Chrome installation
                headless=False,
                args=_STEALTH_ARGS_REAL_CHROME,
                ignore_default_args=["--enable-automation"],
            )
        return pw.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=False,
            args=_STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
            user_agent=_STEALTH_USER_AGENT,
        )

    with _playwright_lock:
        try:
            with sync_playwright() as pw:
                ctx = None
                if platform in _NEEDS_REAL_CHROME:
                    try:
                        ctx = _launch(pw, use_real_chrome=True)
                        logger.info(f"open-login/{platform}: using real Chrome")
                    except Exception as chrome_err:
                        logger.warning(
                            f"open-login/{platform}: real Chrome unavailable ({chrome_err})"
                            " — falling back to bundled Chromium"
                        )
                        ctx = None

                if ctx is None:
                    ctx = _launch(pw, use_real_chrome=False)
                    ctx.add_init_script(_STEALTH_SCRIPT)

                page = ctx.new_page()

                warmup = _WARMUP_URLS.get(platform)
                if warmup:
                    try:
                        page.goto(warmup, timeout=20000, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    time.sleep(4)

                page.goto(_LOGIN_OPEN_URLS[platform], timeout=20000, wait_until="domcontentloaded")

                # Wait up to 10 minutes — user needs time to complete login + CAPTCHA
                try:
                    page.wait_for_url(_is_logged_in, timeout=600_000)
                except Exception:
                    pass
                time.sleep(5)   # let session cookies fully flush to disk
                ctx.close()
        except Exception as e:
            logger.error(f"open-login/{platform}: {e}")


@app.post("/api/track-job")
async def track_job(body: dict):
    """
    Called by the Chrome extension when the user clicks 'Send to Job Agent'
    on a job listing page. Stores the URL so the agent can queue it.
    """
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    # Log it so it shows in the live log stream and can be picked up later
    msg = f"[extension] Tracked job: {body.get('title', 'Unknown')} @ {body.get('company', '')} — {url}"
    logger.info(msg)
    log_buffer.append({"level": "info", "message": msg, "timestamp": datetime.now().isoformat()})
    await manager.broadcast(json.dumps({"level": "info", "message": msg, "timestamp": datetime.now().isoformat()}))
    return {"ok": True, "message": "Job tracked — it will appear in your next search run."}


@app.post("/api/open-login/{platform}")
async def open_login(platform: str):
    """Open a visible Chromium window so the user can log in and save the session."""
    if platform not in _LOGIN_OPEN_URLS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    if _playwright_lock.locked():
        msg = "Browser is busy (login check in progress). It will open in a few seconds."
        return {"ok": True, "message": msg}

    t = threading.Thread(target=_open_login_sync, args=(platform,), daemon=True)
    t.start()
    return {"ok": True, "message": f"Opening {platform} login window…"}


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket endpoint for real-time logs"""
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await manager.disconnect(websocket)

@app.get("/frontend/index.html")
@app.get("/")
async def serve_frontend():
    """Serve the frontend HTML"""
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return {"error": "Frontend HTML not found. Make sure web/frontend/index.html exists."}

# ── Background Tasks ──

def _job_to_dict(job) -> dict:
    """Serialize a JobPosting to the dict shape the frontend expects."""
    bd = job.score_breakdown or {}
    return {
        "id": str(job.id),
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "url": job.url,
        "fit_score": float(job.fit_score or 0),
        "salary_score": float(job.salary_score or 0),
        "combined_score": float(job.combined_score or 0),
        "status": "found",
        "date_found": datetime.now().isoformat(),
        "match_reasons": bd.get("match_reasons", []),
        "gap_reasons": bd.get("gap_reasons", []),
        "recommended_keywords": bd.get("recommended_keywords", []),
    }


def _write_role_scout_to_vault(recs: list, vault_notes: int, profile_name: str, vault_path: str) -> str:
    """
    Write a Role Scout session to the Obsidian vault.
    Creates: {vault}/Job Agent/Role Scout/YYYY-MM-DD HH-MM.md

    Each scan is a separate dated note so the history accumulates over time —
    the vault grows a longitudinal record of what the system thinks the user
    can land and what the gaps were, useful for tracking career progression.
    """
    from pathlib import Path as _P
    vault = _P(vault_path)
    if not vault.exists():
        return ""

    folder = vault / "Job Agent" / "Role Scout"
    folder.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    date_str  = now.strftime("%Y-%m-%d")
    time_str  = now.strftime("%H:%M")
    safe_ts   = now.strftime("%Y-%m-%d %H-%M")
    note_path = folder / f"{safe_ts}.md"

    conf_labels = {"strong": "◆ STRONG FIT", "solid": "▲ SOLID FIT", "possible": "◇ POSSIBLE"}

    def _rec_block(r: dict, rank: int) -> str:
        title      = r.get("title", "Unknown")
        salary     = r.get("salary_range", "—")
        conf       = r.get("confidence", "possible")
        why        = r.get("why_qualified", "—")
        gap        = r.get("gap_to_close", "")
        vault_sig  = r.get("vault_signal", "")
        terms      = r.get("search_terms", [])
        salary_mid = r.get("salary_mid", 0)

        lines = [
            f"### #{rank} · {title} · {salary}",
            f"**Confidence:** {conf_labels.get(conf, conf.upper())}  ",
            f"**Salary mid:** ${salary_mid:,}" if salary_mid else "",
            f"**Why qualified:** {why}  ",
        ]
        if gap and gap.lower() not in ("none", "") and "already competitive" not in gap.lower():
            lines.append(f"**Gap to close:** {gap}  ")
        if vault_sig and vault_sig != "general experience":
            lines.append(f"**Vault signal:** {vault_sig}  ")
        if terms:
            lines.append(f"**Search terms:** {' · '.join(terms)}")
        return "\n".join(l for l in lines if l)

    rec_blocks = "\n\n".join(_rec_block(r, i + 1) for i, r in enumerate(recs))

    content = f"""---
tags: [job-agent, role-scout, career-intel]
date: {date_str}
vault_notes_analyzed: {vault_notes}
roles_found: {len(recs)}
top_role: {recs[0].get('title', '') if recs else ''}
top_salary_mid: {recs[0].get('salary_mid', 0) if recs else 0}
---

# Role Scout · {date_str} {time_str}

> Evidence-based job title recommendations for **{profile_name}** generated by Job Agent.
> Analyzed **{vault_notes}** vault notes + full work history. Ranked by realistic compensation.

## Top Roles by Compensation

{rec_blocks}

---
*Generated by Job Agent Vault Scout · {date_str} {time_str}*
"""
    try:
        note_path.write_text(content, encoding="utf-8")
        logger.info(f"[vault] Role Scout note: {note_path.name}")
    except Exception as e:
        logger.warning(f"[vault] Role Scout note write failed: {e}")
        return ""
    return str(note_path)


def _write_resume_to_vault(tailored, vault_path: str) -> str:
    """
    Write a markdown note to the Obsidian vault documenting a tailored resume.
    Creates: {vault}/Job Applications/Resumes/YYYY-MM-DD_Company_Title.md
    """
    from pathlib import Path as _P
    vault = _P(vault_path)
    if not vault.exists():
        return ""
    folder = vault / "Job Applications" / "Resumes"
    folder.mkdir(parents=True, exist_ok=True)

    job = tailored.job
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_co = "".join(c for c in (job.company or "") if c.isalnum() or c in " _-")[:20].strip().replace(" ", "_")
    safe_title = "".join(c for c in (job.title or "") if c.isalnum() or c in " _-")[:25].strip().replace(" ", "_")
    note_path = folder / f"{date_str}_{safe_co}_{safe_title}.md"

    docx_name = _P(tailored.docx_path).name if tailored.docx_path else ""
    resume_link = f"[[{docx_name}]]" if docx_name else "—"
    skills_block = "\n".join(f"- {s}" for s in (tailored.highlighted_skills or [])[:15])
    kw_block = ", ".join(tailored.keywords_matched[:20]) if tailored.keywords_matched else "—"

    # Build experience section from tailored roles (top 3, top 2 bullets each)
    exp_lines = []
    for role in (tailored.tailored_experience or [])[:3]:
        header = f"### {role.title} @ {role.company}"
        if role.start_date or role.end_date:
            header += f" ({role.start_date or '?'} – {role.end_date or 'Present'})"
        bullets = "\n".join(f"- {a}" for a in (role.achievements or [])[:2])
        exp_lines.append(header + "\n" + (bullets or f"- {role.description or ''}"))
    exp_block = "\n\n".join(exp_lines) if exp_lines else "—"

    ats_score = round(tailored.ats_score_estimate or 0)

    content = f"""# {job.title} @ {job.company}

**Date:** {date_str}
**URL:** {job.url or '—'}
**Fit Score:** {round(job.fit_score or 0)}/100
**ATS Score Estimate:** {ats_score}/100
**Resume File:** {resume_link}

## Tailored Summary

{tailored.tailored_summary or '—'}

## Key Experience (Tailored Order)

{exp_block}

## Highlighted Skills

{skills_block or '—'}

## Keywords Matched

{kw_block}
"""
    try:
        note_path.write_text(content, encoding="utf-8")
        logger.info(f"[vault] Resume note: {note_path.name}")
    except Exception as e:
        logger.warning(f"[vault] Resume note write failed: {e}")
        return ""
    return str(note_path)


def _write_session_to_vault(run_results: list, run_score: dict, vault_path: str) -> str:
    """
    Write a session summary note to Obsidian after each auto-apply run.
    Creates: {vault}/Job Applications/Sessions/YYYY-MM-DD HH-MM Run.md
    """
    from pathlib import Path as _P
    vault = _P(vault_path)
    if not vault.exists():
        return ""
    folder = vault / "Job Applications" / "Sessions"
    folder.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    note_path = folder / f"{now.strftime('%Y-%m-%d %H-%M')} Run.md"

    score    = run_score.get("score", 0)
    xp       = run_score.get("xp_earned", 0)
    applied  = run_score.get("applied", 0)
    manual   = run_score.get("manual", 0)
    failed   = run_score.get("failed", 0)
    attempted = run_score.get("attempted", 0)
    score_emoji = "🟢" if score >= 70 else "🟡" if score >= 40 else "🔴"

    def _entry(r):
        title   = r.get("title", "Unknown")
        company = r.get("company", "")
        url     = r.get("url", "")
        err     = (r.get("error") or "")[:100]
        rp      = r.get("resume_path", "")
        rname   = _P(rp).name if rp else ""
        link    = f"[{title}]({url})" if url else title
        bits    = [f"**{company}**", link]
        if rname:
            bits.append(f"resume: [[{rname}]]")
        if err:
            bits.append(f"_{err}_")
        return "- " + " — ".join(bits)

    applied_rows  = [r for r in run_results if r.get("status") == "applied"]
    manual_rows   = [r for r in run_results if r.get("status") == "needs_manual"]
    failed_rows   = [r for r in run_results if r.get("status") not in ("applied", "needs_manual")]

    def _section(rows):
        return ("\n".join(_entry(r) for r in rows) + "\n") if rows else "_none_\n"

    content = f"""# Application Run — {now.strftime("%B %d, %Y %H:%M")}

{score_emoji} **Score: {score}/100** | +{xp} XP | {attempted} jobs attempted

| Metric | Count |
|--------|-------|
| ✓ Applied | {applied} |
| ⚠ Needs Manual | {manual} |
| ✗ Failed / Error | {failed} |

## ✓ Applied ({applied})

{_section(applied_rows)}
## ⚠ Needs Manual ({manual})

{_section(manual_rows)}
## ✗ Failed ({failed})

{_section(failed_rows)}
"""
    try:
        note_path.write_text(content, encoding="utf-8")
        logger.info(f"[vault] Session note: {note_path.name}")
    except Exception as e:
        logger.warning(f"[vault] Session note write failed: {e}")
        return ""
    return str(note_path)


async def run_search_background(settings: Optional[SearchSettings]):
    """Run job search in background, scoring and streaming jobs in batches of 10."""
    global pipeline_running, current_step

    async def log(msg: str, type_: str = "log"):
        await manager.broadcast(json.dumps({
            "type": type_,
            "message": msg,
            "timestamp": datetime.now().isoformat(),
        }))

    try:
        current_step = "Loading Profile..."
        await log("[SEARCH] Starting job search...")

        # Load profile from in-memory cache or disk — never calls Claude here
        profile = agent._load_profile_cache() if not agent.profile else agent.profile
        if not profile:
            await log("[ERROR] No profile found. Go to the Profile tab and click 'Rescan Profile' first.", "error")
            return

        await log(f"[PROFILE] Loaded profile for {profile.name}")

        current_step = "Searching Jobs..."
        await log("[SEARCH] Scanning job boards (this may take a minute)...")

        # Run the blocking scrape in a thread so WebSocket messages keep flowing
        jobs = await asyncio.to_thread(agent.searcher.search_all)

        await log(f"[SEARCH] Found {len(jobs)} jobs — scoring in batches of 10...")
        current_step = "Scoring & Saving..."

        new_count = 0
        total = len(jobs)

        # Score in batches of 10, save + broadcast each batch immediately
        for i in range(0, total, 10):
            batch = jobs[i:i + 10]

            # Blocking Claude call → runs in thread so WS stays alive
            scored_batch = await asyncio.to_thread(agent.scorer._score_batch, batch, profile)

            # Apply country filter before saving
            allowed_countries = (config.search.allowed_countries or []) if config else []
            if allowed_countries:
                scored_batch = [j for j in scored_batch if _location_allowed(j.location, allowed_countries)]

            saved = []
            for job in scored_batch:
                try:
                    if agent.tracker.upsert_job(job):
                        new_count += 1
                    saved.append(job)
                except Exception as save_err:
                    logger.warning(f"Failed to save job {job.id}: {save_err}")

            done = min(i + 10, total)
            await log(f"[SCORE] {done}/{total} scored — {new_count} new jobs so far")

            # Push job cards to the frontend in real-time
            if saved:
                await manager.broadcast(json.dumps({
                    "type": "jobs",
                    "jobs": [_job_to_dict(j) for j in saved],
                    "timestamp": datetime.now().isoformat(),
                }))

        await log(f"[DONE] Search complete — {new_count} new jobs saved!", "complete")

    except Exception as e:
        logger.error(f"Search error: {e}")
        await log(f"[ERROR] {e}", "error")
    finally:
        pipeline_running = False
        current_step = None


async def run_auto_apply_background(min_score: float, max_apply: int):
    """Apply to scored jobs. Streams results live. Never re-applies to the same job."""
    global pipeline_running, current_step, _run_log
    import uuid as _uuid
    from job_agent.models import Application, ApplicationStatus, JobPosting, JobPlatform, TailoredResume
    from job_agent.builders.resume_builder import build_resume_docx
    from job_agent.automation.ats_handlers import detect_ats as _detect_ats

    cfg = load_config()
    if _run_log is None:
        _run_log = RunLog(str(Path(cfg.output.output_dir) / "run_log.md"))

    async def log(msg: str, type_: str = "log"):
        await manager.broadcast(json.dumps({
            "type": type_, "message": msg,
            "timestamp": datetime.now().isoformat(),
        }))

    async def push_apply_update(job_id: str, status: str, company: str, title: str,
                                 error: str = "", notes: str = ""):
        await manager.broadcast(json.dumps({
            "type": "apply_update",
            "job_id": job_id,
            "status": status,
            "company": company,
            "title": title,
            "error": error,
            "notes": notes,
            "timestamp": datetime.now().isoformat(),
        }))

    _run_start_time = datetime.now().isoformat()

    try:
        current_step = "Auto-Apply: Loading profile..."
        await log("[APPLY] Starting auto-apply pipeline...")

        profile = agent._load_profile_cache() if not agent.profile else agent.profile
        if not profile:
            await log("[ERROR] No profile found — run Deep Rescan on the Profile tab first.", "error")
            return

        await log(f"[APPLY] Profile loaded for {profile.name}")
        current_step = "Auto-Apply: Selecting jobs..."

        def _applyability_bonus(row: dict) -> float:
            """
            Extra pts based on how automatable this job is.
            LinkedIn Easy Apply and direct ATS jobs queue first — Indeed last.
            Run data: LinkedIn 50%+ success, Indeed has CAPTCHA + browser crash issues.
            """
            platform = str(row.get("platform", "")).lower()
            url = str(row.get("url", ""))
            if platform == "linkedin":
                base = 20.0   # LinkedIn Easy Apply: best real-world success rate
            elif platform == "indeed":
                base = 5.0    # Indeed: CAPTCHA-prone, browser crashes; queue last
            else:
                base = 8.0    # Direct company ATS links: reliable once navigated
            ats = _detect_ats(url)
            if ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "bamboohr"):
                base += 12.0  # Known ATS — dedicated fill handler, high fill rate
            elif ats == "workday":
                base += 4.0   # Workday works but is complex
            elif ats == "metacareers":
                base -= 20.0  # Meta Careers = not automatable; needs manual
            elif ats == "generic":
                base -= 2.0   # Unknown ATS — slight penalty
            return base

        # Get top jobs not yet attempted — deduplicate by job_id within this run
        rows = tracker.get_jobs(min_score=min_score, limit=200)
        # Re-rank by combined_score + recency_bonus + applyability so reliable ATS jobs surface first
        for row in rows:
            row["applyability_bonus"] = _applyability_bonus(row)
            row["queue_score"] = row.get("final_score", row.get("combined_score", 0)) + row["applyability_bonus"]
        rows.sort(key=lambda r: r["queue_score"], reverse=True)

        manual_only_set = set(x.lower() for x in cfg.automation.manual_only_platforms)
        candidates = []
        queued_ids: set = set()  # prevent same job queued twice in one run
        for row in rows:
            jid = str(row["id"])
            if jid in queued_ids:
                continue
            if str(row.get("platform", "")).lower() in manual_only_set:
                continue
            if tracker.already_applied(jid):
                continue
            if tracker.failure_count(jid) >= 3:
                await log(f"[SKIP] {row.get('title','')} @ {row.get('company','')} — 3+ failures, skipping permanently")
                continue
            if tracker.needs_manual_count(jid) >= 2:
                await log(f"[SKIP] {row.get('title','')} @ {row.get('company','')} — 2+ needs_manual blocks, skipping permanently")
                continue
            queued_ids.add(jid)
            candidates.append(row)
            if len(candidates) >= max_apply:
                break

        if not candidates:
            await log("[APPLY] No new jobs to apply to — all scored jobs have already been attempted.", "complete")
            return

        await log(f"[APPLY] {len(candidates)} jobs queued for auto-apply (min score {min_score:.0f})")
        _run_log.start_run(f"{len(candidates)} jobs queued (min score {min_score:.0f})")
        _stop_event.clear()  # arm the stop gate for this run

        applied_count = 0
        needs_manual_count = 0
        failed_count = 0
        run_results: list = []  # per-job outcome list for vault session note
        job_scores: list = [row.get("combined_score", 0) for row in candidates]

        for i, row in enumerate(candidates):
            if _stop_event.is_set():
                await log(f"[APPLY] Stop requested — halting after {i} jobs", "complete")
                break
            job_id = str(row["id"])
            title = row.get("title", "")
            company = row.get("company", "")
            current_step = f"Applying: {company} ({i+1}/{len(candidates)})"
            await log(f"[APPLY] [{i+1}/{len(candidates)}] {title} @ {company}")

            # Build JobPosting from DB row
            job = JobPosting(
                id=job_id,
                title=title,
                company=company,
                location=row.get("location") or "",
                description=row.get("description") or "",
                url=row.get("url") or "",
                platform=JobPlatform(row["platform"]) if row.get("platform") else JobPlatform.INDEED,
                salary_min=row.get("salary_min"),
                salary_max=row.get("salary_max"),
                fit_score=row.get("fit_score") or 0,
                combined_score=row.get("combined_score") or 0,
            )

            # Tailor resume for this specific job
            try:
                current_step = f"Tailoring resume: {company}"
                await log(f"[APPLY] Tailoring resume for {company}...")
                tailored = await asyncio.wait_for(
                    asyncio.to_thread(agent.tailor.tailor, job, profile, vault_index=agent.vault_index),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                te = "Resume tailor timed out"
                await log(f"[APPLY] {te} for {company}", "error")
                tailored = TailoredResume(
                    job=job, profile=profile,
                    tailored_summary=profile.summary,
                    highlighted_skills=profile.skills[:10],
                )
            except Exception as te:
                await log(f"[APPLY] Resume tailor failed for {company}: {te}", "error")
                tailored = TailoredResume(
                    job=job, profile=profile,
                    tailored_summary=profile.summary,
                    highlighted_skills=profile.skills[:10],
                )

            # Generate DOCX
            try:
                await asyncio.to_thread(build_resume_docx, tailored, config.output.resumes_dir)
            except Exception:
                pass  # DOCX failure doesn't block the apply attempt

            # Save resume note to Obsidian vault and record the path
            vault_note_path = ""
            if cfg.profile.obsidian_vault_path:
                try:
                    vault_note_path = await asyncio.to_thread(
                        _write_resume_to_vault, tailored, cfg.profile.obsidian_vault_path
                    ) or ""
                except Exception as _ve:
                    logger.warning(f"[vault] resume note: {_ve}")

            # Create the application record before attempting
            app_obj = Application(
                id=str(_uuid.uuid4()),
                job=job,
                resume=tailored,
                status=ApplicationStatus.QUEUED,
            )
            app_id = tracker.create_application(app_obj)
            app_obj.id = app_id
            # Store vault note path if written
            if vault_note_path:
                tracker.update_application(app_id, vault_note_path=vault_note_path)

            # Attempt the auto-apply in a thread (Playwright is blocking)
            try:
                current_step = f"Submitting: {company}"
                result = await asyncio.wait_for(
                    asyncio.to_thread(agent.agent.apply_one, app_obj),
                    timeout=600.0,  # 10 min hard cap per application
                )
                tracker.sync_application(result)

                if result.status == ApplicationStatus.APPLIED:
                    applied_count += 1
                    await log(f"[APPLY] ✓ APPLIED — {title} @ {company}", "complete")
                    await push_apply_update(job_id, "applied", company, title,
                                            notes=result.notes or "")
                elif result.status == ApplicationStatus.NEEDS_MANUAL:
                    needs_manual_count += 1
                    await log(f"[APPLY] ⚠ NEEDS MANUAL — {company}: {result.error}")
                    await push_apply_update(job_id, "needs_manual", company, title,
                                            error=result.error, notes=result.notes or "")
                    # Broadcast assist request so frontend shows the help overlay
                    from job_agent.db.improvement_tracker import _classify_error
                    _job_url = row.get("url", "")
                    _assist_needed_info.update({
                        "job_id": job_id,
                        "job_title": title,
                        "company": company,
                        "url": _job_url,
                        "ats": _detect_ats(_job_url),
                        "error": result.error or "",
                        "error_type": _classify_error(result.error or ""),
                    })
                    await manager.broadcast(json.dumps({
                        "type": "assist_needed",
                        **_assist_needed_info,
                        "timestamp": datetime.now().isoformat(),
                    }))
                else:
                    failed_count += 1
                    await log(f"[APPLY] ✗ FAILED — {company}: {result.error}", "error")
                    await push_apply_update(job_id, "failed", company, title,
                                            error=result.error)

                # Write to run log immediately after each result
                _run_log.log_result(
                    title=title,
                    company=company,
                    url=row.get("url", ""),
                    status=result.status.value,
                    ats=_detect_ats(row.get("url", "")),
                    error=result.error,
                    notes=result.notes,
                    fields_filled=len(result.form_data) if result.form_data else 0,
                )
                run_results.append({
                    "title": title, "company": company, "url": row.get("url", ""),
                    "status": result.status.value,
                    "error": result.error or "",
                    "notes": result.notes or "",
                    "resume_path": tailored.docx_path or "",
                })

            except asyncio.TimeoutError:
                # 10-minute hard cap hit — treat as needs_manual so the job stays retryable
                needs_manual_count += 1
                err_msg = "Apply timed out (10 min cap) — apply manually"
                logger.warning(f"Apply timeout for {company} — marking needs_manual")
                tracker.update_application(app_obj.id, status="needs_manual", error=err_msg)
                await log(f"[APPLY] ⚠ TIMEOUT — {company}: exceeded 10 min", "error")
                await push_apply_update(job_id, "needs_manual", company, title, error=err_msg)
                _run_log.log_result(
                    title=title, company=company,
                    url=row.get("url", ""), status="needs_manual", error=err_msg,
                )
                run_results.append({
                    "title": title, "company": company, "url": row.get("url", ""),
                    "status": "needs_manual", "error": err_msg, "notes": "",
                    "resume_path": tailored.docx_path or "",
                })
            except Exception as e:
                failed_count += 1
                logger.error(f"Apply exception for {company}: {e}")
                tracker.update_application(app_obj.id, status="failed", error=str(e))
                await log(f"[APPLY] ✗ ERROR — {company}: {e}", "error")
                await push_apply_update(job_id, "failed", company, title, error=str(e))
                _run_log.log_result(
                    title=title, company=company,
                    url=row.get("url", ""), status="failed", error=str(e),
                )
                run_results.append({
                    "title": title, "company": company, "url": row.get("url", ""),
                    "status": "error", "error": str(e), "notes": "",
                    "resume_path": tailored.docx_path or "",
                })

        _run_log.finish_run()

        # Build error breakdown for system analysis
        from job_agent.db.improvement_tracker import ImprovementTracker, _classify_error
        error_breakdown: dict = {}
        for r in run_results:
            if r["status"] in ("failed", "error", "needs_manual") and r.get("error"):
                etype = _classify_error(r["error"])
                error_breakdown[etype] = error_breakdown.get(etype, 0) + 1

        # Save run score with dimensional analysis
        run_result = tracker.save_run_score(
            started_at=_run_start_time,
            ended_at=datetime.now().isoformat(),
            applied=applied_count,
            manual=needs_manual_count,
            failed=failed_count,
            label=f"{len(candidates)} jobs queued",
            job_scores=job_scores,
            error_breakdown=error_breakdown,
        )
        score_summary = tracker.get_score_summary()

        # Pull improvement ROI list and log system analysis to run_log
        _imp = ImprovementTracker(str(cfg.output.db_path))
        top_fixes = _imp.get_improvement_roi(limit=5)
        _run_log.log_system_analysis(
            score=run_result["score"],
            grade=run_result["grade"],
            delta=run_result["delta"],
            breakdown=run_result["breakdown"],
            top_fixes=top_fixes,
        )

        # Write session summary to Obsidian vault
        if cfg.profile.obsidian_vault_path:
            try:
                await asyncio.to_thread(
                    _write_session_to_vault, run_results, run_result, cfg.profile.obsidian_vault_path
                )
            except Exception as _ve:
                logger.warning(f"[vault] session note: {_ve}")

        await manager.broadcast(json.dumps({
            "type":        "score_update",
            "run_score":   run_result["score"],
            "grade":       run_result["grade"],
            "delta":       run_result["delta"],
            "breakdown":   run_result["breakdown"],
            "top_fixes":   top_fixes[:3],
            "xp_earned":   run_result["xp_earned"],
            "total_xp":    score_summary["total_xp"],
            "level":       score_summary["level"],
            "streak":      score_summary["streak"],
            "best_score":  score_summary["best_score"],
        }))

        summary = (f"[DONE] Auto-apply complete — "
                   f"{applied_count} applied, {needs_manual_count} need manual, "
                   f"{failed_count} failed | Score: {run_result['score']}/100 | "
                   f"+{run_result['xp_earned']} XP")
        await log(summary, "complete")

    except Exception as e:
        logger.error(f"Auto-apply pipeline error: {e}")
        await log(f"[ERROR] {e}", "error")
    finally:
        pipeline_running = False
        current_step = None


# ── Job Scan (search + score, no apply) ──────────────────────────────────────

@app.post("/api/scan-jobs")
async def scan_jobs(min_score: float = 60.0):
    """
    Run the search + score pipeline without applying.
    Discovers new jobs, scores them, saves to DB. Returns count of new jobs found.
    """
    global pipeline_running, current_step
    if pipeline_running:
        raise HTTPException(status_code=409, detail="Pipeline already running")
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    pipeline_running = True
    current_step = "Scanning for new jobs..."
    asyncio.create_task(_run_scan_background(min_score))
    return {"status": "started", "min_score": min_score}


async def _run_scan_background(min_score: float):
    """Search + score new jobs, save to DB, broadcast progress."""
    global pipeline_running, current_step

    async def log(msg: str, type_: str = "log"):
        await manager.broadcast(json.dumps({
            "type": type_, "message": msg,
            "timestamp": datetime.now().isoformat(),
        }))

    try:
        await log("[SCAN] Starting job scan...")
        profile = agent._load_profile_cache() if not agent.profile else agent.profile
        if not profile:
            await log("[ERROR] No profile found — build profile first.", "error")
            return

        await log(f"[SCAN] Searching across all platforms...")
        jobs = await asyncio.to_thread(agent.searcher.search_all)
        await log(f"[SCAN] Found {len(jobs)} raw listings — scoring...")

        allowed_countries = (config.search.allowed_countries or []) if config else []
        scored = await asyncio.to_thread(
            agent.scorer.score_batch, jobs, profile, min_score, allowed_countries
        )
        await log(f"[SCAN] {len(scored)} jobs scored ≥ {min_score:.0f}")

        new_count = 0
        for job in scored:
            if await asyncio.to_thread(agent.tracker.upsert_job, job):
                new_count += 1

        await log(
            f"[SCAN] Done — {new_count} new jobs added to queue (skipped {len(scored) - new_count} duplicates)",
            "complete",
        )
        await manager.broadcast(json.dumps({
            "type": "scan_complete",
            "new_count": new_count,
            "total_scored": len(scored),
            "timestamp": datetime.now().isoformat(),
        }))
    except Exception as e:
        logger.error(f"Scan error: {e}")
        await log(f"[ERROR] Scan failed: {e}", "error")
    finally:
        pipeline_running = False
        current_step = None


# ── Run Log ───────────────────────────────────────────────────────────────────

@app.get("/api/run-log")
async def get_run_log():
    """Return the markdown run log content."""
    try:
        cfg = load_config()
        log_path = Path(cfg.output.output_dir) / "run_log.md"
        if not log_path.exists():
            return {"content": "# Job Agent Run Log\n\nNo runs yet.", "path": str(log_path)}
        content = log_path.read_text(encoding="utf-8")
        return {"content": content, "path": str(log_path)}
    except Exception as e:
        return {"content": f"# Error\n\n{e}", "path": ""}


@app.delete("/api/run-log")
async def clear_run_log():
    """Clear (reset) the run log file."""
    try:
        cfg = load_config()
        log_path = Path(cfg.output.output_dir) / "run_log.md"
        log_path.write_text("# Job Agent Run Log\n\n", encoding="utf-8")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Fix Proposals (auto-review loop) ─────────────────────────────────────────

PROPOSALS_PATH = Path(__file__).parent.parent.parent / "output" / "fix_proposals.md"

@app.get("/api/fix-proposals")
async def get_fix_proposals():
    """Return pending fix proposals written by the autonomous log-review loop."""
    if not PROPOSALS_PATH.exists():
        return {"has_proposals": False, "content": "", "generated_at": ""}
    try:
        content = PROPOSALS_PATH.read_text(encoding="utf-8")
        stat = PROPOSALS_PATH.stat()
        from datetime import datetime as _dt
        generated_at = _dt.fromtimestamp(stat.st_mtime).isoformat()
        has_pending = "## Pending" in content and content.count("### FIX-") > 0
        return {
            "has_proposals": has_pending,
            "content": content,
            "generated_at": generated_at,
        }
    except Exception as e:
        return {"has_proposals": False, "content": "", "generated_at": "", "error": str(e)}


@app.post("/api/fix-proposals/dismiss")
async def dismiss_fix_proposals():
    """Mark all current proposals as reviewed (rename to .reviewed so notification clears)."""
    try:
        if PROPOSALS_PATH.exists():
            content = PROPOSALS_PATH.read_text(encoding="utf-8")
            # Replace ## Pending with ## Reviewed so the badge clears
            updated = content.replace("## Pending", "## Reviewed")
            PROPOSALS_PATH.write_text(updated, encoding="utf-8")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Shutdown ─────────────────────────────────────────────────────────────────

_shutdown_in_progress = False


async def _graceful_shutdown(source: str = "api") -> list[str]:
    """Save all in-flight progress before the process exits.

    Returns a list of human-readable save steps that completed.
    Safe to call twice — idempotent via _shutdown_in_progress guard.
    """
    global pipeline_running, _shutdown_in_progress

    if _shutdown_in_progress:
        return ["already shutting down"]
    _shutdown_in_progress = True

    saved = []
    logger.info(f"[shutdown:{source}] Saving progress…")

    # 1. Stop the apply pipeline after its current job
    if pipeline_running:
        _stop_event.set()
        pipeline_running = False
        saved.append("pipeline stopped")
        logger.info("[shutdown] Pipeline stop signal sent")

    # 2. Flush run log if a run was active mid-flight
    if _run_log is not None and _run_log._run_start is not None:
        try:
            _run_log.log_issue(
                "Server shutdown",
                "Progress saved up to this point — incomplete run finalized",
            )
            _run_log.finish_run()
            saved.append("run log saved")
            logger.info("[shutdown] Run log finalized")
        except Exception as e:
            logger.warning(f"[shutdown] Run log flush failed: {e}")

    # 3. Write improvement tracker note to vault (best-effort)
    if agent is not None and config is not None:
        try:
            vault_path = config.profile.obsidian_vault_path
            if vault_path:
                note_path = agent._improvement_tracker.write_vault_note(vault_path)
                if note_path:
                    saved.append("vault note updated")
                    logger.info(f"[shutdown] Improvement log → {note_path}")
        except Exception as e:
            logger.warning(f"[shutdown] Vault note failed: {e}")

    # SQLite auto-commits after every write — no explicit flush needed
    # Profile cache saved on build — no flush needed

    logger.info(f"[shutdown:{source}] Done. Saved: {saved}")
    return saved


@app.post("/api/shutdown")
async def shutdown_server():
    """Gracefully save progress, then stop the server.
    Saves: active run log, pipeline state, vault improvement note.
    Hard-exits after 400ms so the HTTP response returns first.
    """
    saved = await _graceful_shutdown(source="api")

    import threading

    def _exit():
        import time
        time.sleep(0.4)
        import os
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
    return {"status": "shutting_down", "saved": saved}


# ── Score / Gamification ─────────────────────────────────────────────────────

@app.get("/api/score")
async def get_score():
    """Return XP, level, streak, best score and recent run history for the dashboard."""
    if not tracker:
        return {"total_xp": 0, "level": 1, "streak": 0, "best_score": 0,
                "last_score": 0, "total_applied": 0, "success_rate": 0, "history": []}
    try:
        return tracker.get_score_summary()
    except Exception as e:
        logger.warning(f"score endpoint: {e}")
        return {"total_xp": 0, "level": 1, "streak": 0, "best_score": 0,
                "last_score": 0, "total_applied": 0, "success_rate": 0, "history": []}


# ── Autopilot ────────────────────────────────────────────────────────────────

_autopilot_enabled: bool = False


class AutopilotRequest(BaseModel):
    enabled: bool


@app.get("/api/autopilot")
async def get_autopilot():
    return {"enabled": _autopilot_enabled}


@app.post("/api/autopilot")
async def set_autopilot(req: AutopilotRequest):
    global _autopilot_enabled
    _autopilot_enabled = req.enabled
    logger.info(f"Autopilot {'enabled' if _autopilot_enabled else 'disabled'}")
    return {"enabled": _autopilot_enabled}


# ── Momentum Score ────────────────────────────────────────────────────────────

@app.get("/api/momentum")
async def get_momentum():
    """
    0-100 score measuring how actively the system is generating interview opportunities.
    Factors: recency of last run, apply rate, total applied last 7 days, automation rate.
    """
    if not tracker:
        return {"score": 0, "summary": "No data yet — run a search to start", "target": ""}
    try:
        from datetime import timedelta
        db_path = Path(config.output.output_dir) / "applications.db"
        conn = sqlite3.connect(str(db_path))
        now = datetime.now()
        week_ago = (now - timedelta(days=7)).isoformat()

        # Jobs found this week
        found_week = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE date_found >= ?", (week_ago,)
        ).fetchone()[0]

        # Applications this week
        applied_week = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE created_at >= ? AND status NOT IN ('dismissed','failed')",
            (week_ago,)
        ).fetchone()[0]

        # Auto-applied (no [Applied manually] in notes)
        auto_applied = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE created_at >= ? AND status='applied' AND (notes IS NULL OR notes NOT LIKE '%manually%')",
            (week_ago,)
        ).fetchone()[0]

        # Total ever applied
        total_applied = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE status NOT IN ('dismissed','failed')"
        ).fetchone()[0]

        conn.close()

        # Score formula (0-100)
        reach_score = min(40, found_week * 2)           # up to 40pts for finding 20+ jobs/week
        apply_score = min(35, applied_week * 7)          # up to 35pts for 5+ applications/week
        automation_score = min(25, auto_applied * 5)     # up to 25pts for automation

        score = reach_score + apply_score + automation_score

        if score == 0:
            summary = "No activity this week — hit Hunt Jobs to start"
        elif score < 30:
            summary = f"{applied_week} applications this week — momentum building"
        elif score < 60:
            summary = f"{applied_week} applied, {found_week} found this week — good pace"
        else:
            summary = f"🔥 {applied_week} applications, {auto_applied} automated — strong momentum"

        target = "Goal: 5+ applications/week with 60%+ automation rate" if score < 80 else ""

        return {"score": score, "summary": summary, "target": target,
                "found_week": found_week, "applied_week": applied_week, "total_applied": total_applied}
    except Exception as e:
        logger.warning(f"momentum: {e}")
        return {"score": 0, "summary": "Could not compute momentum", "target": ""}


# ── Next Action ───────────────────────────────────────────────────────────────

@app.get("/api/next-action")
async def get_next_action():
    """Return the single highest-impact action the user should take right now."""
    try:
        keywords = (config.search.keywords or []) if config and hasattr(config, 'search') else []
        resume = (config.profile.resume_path or '') if config and hasattr(config, 'profile') else ''
        api_key = (config.ai.anthropic_api_key or '') if config and hasattr(config, 'ai') else ''

        if not keywords:
            return {"action": "Add job keywords in Settings → Search to start hunting", "icon": "⚙", "btn_label": "Open Settings", "btn_action": "showTab('settings',document.getElementById('nav-settings'));setTimeout(()=>showSettingsSection('search',document.querySelectorAll('.stab')[1]),50)"}
        if not resume:
            return {"action": "Add your resume path so the agent can auto-fill applications", "icon": "◈", "btn_label": "Add Resume", "btn_action": "showTab('settings',document.getElementById('nav-settings'));setTimeout(()=>showSettingsSection('search',document.querySelectorAll('.stab')[1]),50)"}
        if not api_key:
            return {"action": "Add Anthropic API key to enable AI scoring and form-fill", "icon": "🔑", "btn_label": "Add API Key", "btn_action": "showTab('settings',document.getElementById('nav-settings'));setTimeout(()=>showSettingsSection('platforms',document.querySelectorAll('.stab')[2]),50)"}

        if tracker:
            db_path = Path(config.output.output_dir) / "applications.db"
            conn = sqlite3.connect(str(db_path))
            needs_manual = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='needs_manual'"
            ).fetchone()[0]
            conn.close()
            if needs_manual:
                return {"action": f"{needs_manual} application(s) need your help to complete", "icon": "⚠", "btn_label": "View in War Room", "btn_action": "showTab('war-room',document.getElementById('nav-war-room'))"}

        return {"action": "Run a job search to find new opportunities", "icon": "🎯", "btn_label": "Hunt Jobs", "btn_action": "startSearch()"}
    except Exception as e:
        logger.warning(f"next-action: {e}")
        return {"action": "Run a job search to find new opportunities", "icon": "🎯", "btn_label": "Hunt Jobs", "btn_action": "startSearch()"}


# ── Interview Prep ────────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/interview-prep")
async def generate_interview_prep(job_id: str):
    """Generate 5 interview Q&A pairs for a job and save to Obsidian vault."""
    if not tracker:
        raise HTTPException(status_code=503, detail="Tracker not ready")
    try:
        job = tracker.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        questions = []
        saved_to = None

        # Try AI generation if API key is available
        try:
            import anthropic
            api_key = getattr(config.profile, 'anthropic_api_key', '') or ''
            if api_key:
                client = anthropic.Anthropic(api_key=api_key)
                title = getattr(job, 'title', '') or job.get('title', '') if isinstance(job, dict) else ''
                company = getattr(job, 'company', '') or job.get('company', '') if isinstance(job, dict) else ''
                description = getattr(job, 'description', '') or job.get('description', '') if isinstance(job, dict) else ''
                prompt = f"""Generate 5 interview questions and strong answers for this role:
Title: {title}
Company: {company}
Description (first 500 chars): {description[:500]}

Format as JSON array: [{{"question": "...", "answer": "..."}}]
Focus on behavioral + technical questions most likely to be asked."""
                msg = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1500,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw = msg.content[0].text.strip()
                import re as _re
                m = _re.search(r'\[.*\]', raw, _re.DOTALL)
                if m:
                    questions = json.loads(m.group())
        except Exception as ai_err:
            logger.warning(f"interview-prep AI failed: {ai_err}")
            # Fallback generic questions
            title_str = getattr(job, 'title', 'this role') if not isinstance(job, dict) else job.get('title', 'this role')
            questions = [
                {"question": f"Why are you interested in {title_str}?", "answer": "Focus on alignment with your skills and the company's mission."},
                {"question": "Walk me through your most relevant experience.", "answer": "Use the STAR method: Situation, Task, Action, Result."},
                {"question": "How do you handle competing priorities?", "answer": "Describe your prioritization framework with a concrete example."},
                {"question": "What's your biggest professional achievement?", "answer": "Quantify impact wherever possible."},
                {"question": "Where do you see yourself in 3 years?", "answer": "Align your answer with growth opportunities at this company."},
            ]

        # Save to Obsidian vault
        vault_path = getattr(config.profile, 'obsidian_vault_path', '') or ''
        if vault_path:
            try:
                vault_dir = Path(vault_path) / "Job Agent" / "Interview Prep"
                vault_dir.mkdir(parents=True, exist_ok=True)
                job_title = getattr(job, 'title', 'unknown') if not isinstance(job, dict) else job.get('title', 'unknown')
                company = getattr(job, 'company', 'unknown') if not isinstance(job, dict) else job.get('company', 'unknown')
                safe_name = f"{company} - {job_title}".replace('/', '-').replace('\\', '-')[:60]
                note_path = vault_dir / f"{safe_name}.md"
                lines = [f"# Interview Prep: {job_title} @ {company}", f"Generated: {datetime.now().strftime('%Y-%m-%d')}", ""]
                for i, q in enumerate(questions, 1):
                    lines.append(f"## Q{i}: {q['question']}")
                    lines.append(f"{q['answer']}")
                    lines.append("")
                note_path.write_text('\n'.join(lines), encoding='utf-8')
                saved_to = str(note_path)
            except Exception as vault_err:
                logger.warning(f"interview-prep vault save failed: {vault_err}")

        return {"questions": questions, "saved_to": saved_to}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── War Room Report ───────────────────────────────────────────────────────────

@app.post("/api/war-room-report")
async def generate_war_room_report():
    """Generate a weekly markdown report and save to Obsidian vault."""
    if not tracker:
        raise HTTPException(status_code=503, detail="Tracker not ready")
    try:
        from datetime import timedelta
        db_path = Path(config.output.output_dir) / "applications.db"
        conn = sqlite3.connect(str(db_path))
        now = datetime.now()
        week_ago = (now - timedelta(days=7)).isoformat()

        # Fetch this week's applications with job details
        rows = conn.execute("""
            SELECT j.title, j.company, j.url, a.status, a.created_at, a.notes
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.created_at >= ?
            ORDER BY a.created_at DESC
        """, (week_ago,)).fetchall()

        total = conn.execute("SELECT COUNT(*) FROM applications WHERE status NOT IN ('dismissed','failed')").fetchone()[0]
        interviews = conn.execute("SELECT COUNT(*) FROM applications WHERE status='interview'").fetchone()[0]
        conn.close()

        # Build report
        week_label = now.strftime("Week of %B %d, %Y")
        lines = [
            f"# War Room Report — {week_label}",
            "",
            f"**Total Applications:** {total}  ",
            f"**Active Interviews:** {interviews}  ",
            f"**This Week:** {len(rows)} applications  ",
            "",
            "---",
            "",
            "## This Week's Applications",
            "",
        ]

        if rows:
            for title, company, url, status, created_at, notes in rows:
                date_str = created_at[:10] if created_at else ''
                status_icon = {'applied': '✅', 'interview': '🎯', 'offer': '🏆', 'rejected': '❌', 'needs_manual': '⚠️'}.get(status, '·')
                lines.append(f"- {status_icon} **{title}** @ {company} ({date_str}) [{status}]")
                if notes:
                    lines.append(f"  > {notes[:100]}")
        else:
            lines.append("_No applications this week._")

        lines += [
            "",
            "---",
            "",
            "## Action Items",
            "",
            f"- [ ] Follow up on applications older than 1 week",
            f"- [ ] Research companies with pending applications",
            f"- [ ] Update resume/profile if getting low scores",
            "",
            f"_Generated by Job Agent on {now.strftime('%Y-%m-%d %H:%M')}_",
        ]

        report = '\n'.join(lines)

        # Save to vault
        saved_to = None
        vault_path = getattr(config.profile, 'obsidian_vault_path', '') or ''
        if vault_path:
            try:
                report_dir = Path(vault_path) / "Job Agent" / "War Room Reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                fname = f"War Room {now.strftime('%Y-%m-%d')}.md"
                note_path = report_dir / fname
                note_path.write_text(report, encoding='utf-8')
                saved_to = str(note_path)
            except Exception as ve:
                logger.warning(f"war-room-report vault save: {ve}")

        return {"report": report, "saved_to": saved_to, "week": week_label}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Vault Sync ────────────────────────────────────────────────────────────────

@app.post("/api/vault-sync")
async def vault_sync():
    """Re-scan Obsidian vault and rebuild AI profile context."""
    try:
        vault_path = getattr(config.profile, 'obsidian_vault_path', '') or ''
        if not vault_path or not Path(vault_path).exists():
            raise HTTPException(status_code=400, detail="Vault path not set or not found")
        # Trigger profile rescan (reuses existing rescan logic)
        from job_agent.ai.resume_tailor import ResumeTailor
        tailor = ResumeTailor(config)
        await asyncio.get_event_loop().run_in_executor(None, tailor.build_profile)
        return {"status": "synced", "vault": vault_path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Main ──

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*60)
    print("  JOB AGENT WEB SERVER")
    print("="*60)
    print("\n✨ Open your browser:")
    print("   http://localhost:8000")
    print("\n📡 API Documentation:")
    print("   http://localhost:8000/docs")
    print("\n" + "="*60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
