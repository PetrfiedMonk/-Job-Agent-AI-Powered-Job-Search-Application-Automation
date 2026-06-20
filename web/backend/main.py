"""
Job Agent Web Server - FastAPI Backend
Provides REST API and WebSocket endpoints for the React frontend
"""

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sys
import os

# Add parent directory to path to import job_agent
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_agent.config import load_config
from job_agent.orchestrator import JobOrchestrator
from job_agent.db.tracker import Tracker
from job_agent.db.field_semantics import FieldSemanticsDB
from job_agent.models import JobPosting, JobPlatform
from job_agent.ai.resume_tailor import ResumeTailor
from job_agent.ai.form_filler import SmartFormFiller

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

# ── Global State ──

pipeline_running = False
current_step = None
agent = None
tracker = None
config = None
semantics_db = None
smart_filler = None
log_buffer = []

# CAPTCHA pause-gate (thread-safe): set=green, clear=waiting for human solve
_captcha_event = threading.Event()
_captcha_event.set()          # start in "no CAPTCHA" state
_captcha_job_info: dict = {}  # last CAPTCHA job context — shown in the UI
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

        logger.info("Job Agent initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Job Agent: {e}")

    yield

    logger.info("Shutting down Job Agent Web Server...")

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
            if status and app_status != status:
                continue

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
    """Stop the running pipeline"""
    global pipeline_running
    pipeline_running = False
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
        profile = await asyncio.to_thread(agent.load_profile, True)
        return {
            "status": "ok",
            "name": profile.name,
            "skills_count": len(profile.skills),
            "message": "Profile rebuilt and cached. No tokens will be spent on next startup.",
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
        cover_letter = agent.tailor.generate_cover_letter(job, profile)
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

        response = client.messages.create(
            model=config.ai.scoring_model,
            max_tokens=2048,
            system=OUTREACH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```")

        data = json.loads(raw)
        data["job_id"] = job_id
        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Outreach generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
        return {"job_id": job_id, "status": "applied"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/jobs/{job_id}/status")
async def update_job_status(job_id: str, status: str):
    """Manually update the application status for a job."""
    valid = {"found", "applied", "interview", "offer", "rejected", "withdrawn", "needs_manual"}
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


# ── Config Read / Write ──

CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"
BROWSER_PROFILE_DIR = Path(__file__).parent.parent.parent / "output" / "browser_profile"

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
        # Reload config in memory
        global config
        config = load_config(str(CONFIG_PATH))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── WebSocket for Real-time Logs ──

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    
    async def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        dead = []
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.warning(f"Failed to send WebSocket message: {e}")
                dead.append(connection)
        for connection in dead:
            await self.disconnect(connection)

manager = ConnectionManager()


def _captcha_notify_fn(info: dict):
    """
    Called synchronously from the ApplicationAgent thread when a CAPTCHA is detected.
    Schedules a WebSocket broadcast on the main asyncio event loop.
    """
    global _captcha_job_info
    _captcha_job_info = info
    if _main_loop and not _main_loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(json.dumps({
                **info,
                "timestamp": datetime.now().isoformat(),
            })),
            _main_loop,
        )


@app.post("/api/captcha-solved")
async def captcha_solved():
    """
    Called by the frontend when the user has manually solved the CAPTCHA.
    Sets the threading.Event so the paused agent thread resumes.
    """
    _captcha_event.set()
    await manager.broadcast(json.dumps({
        "type": "captcha_resolved",
        "timestamp": datetime.now().isoformat(),
    }))
    return {"ok": True, "message": "Resuming auto-apply..."}


@app.get("/api/captcha-status")
async def captcha_status():
    """Returns whether the bot is currently paused waiting for a CAPTCHA solve."""
    return {
        "waiting": not _captcha_event.is_set(),
        "job_info": _captcha_job_info,
    }


# ── Platform Login Status ──────────────────────────────────────────────────────

_LOGIN_URLS = {
    "linkedin": "https://www.linkedin.com/feed/",
    "indeed":   "https://www.indeed.com/",
}

_LOGIN_CHECK_URLS = {
    "linkedin": "https://www.linkedin.com/feed/",
    "indeed":   "https://www.indeed.com/account/login",
}

_LOGIN_OPEN_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "indeed":   "https://secure.indeed.com/account/login",
}


@app.get("/api/login-status")
async def get_login_status():
    """Check login state for each platform using the persistent browser profile."""
    from playwright.async_api import async_playwright

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    result = {"linkedin": False, "indeed": False}

    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(BROWSER_PROFILE_DIR),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )

            # LinkedIn: /feed/ only loads if logged in
            try:
                page = await ctx.new_page()
                await page.goto("https://www.linkedin.com/feed/", timeout=20000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                result["linkedin"] = "/feed" in page.url and "login" not in page.url
                await page.close()
            except Exception as ex:
                logger.debug(f"LinkedIn check error: {ex}")

            # Indeed: if login page redirects us to home, we're in
            try:
                page = await ctx.new_page()
                await page.goto("https://www.indeed.com/account/login", timeout=20000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                # Logged in → redirected away from /account/login
                result["indeed"] = "account/login" not in page.url
                await page.close()
            except Exception as ex:
                logger.debug(f"Indeed check error: {ex}")

            await ctx.close()
    except Exception as e:
        logger.error(f"Login status check failed: {e}")

    return result


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
    """Open a visible browser window so the user can log into a platform and save the session."""
    if platform not in _LOGIN_OPEN_URLS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    from playwright.async_api import async_playwright

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async def _run():
        try:
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    str(BROWSER_PROFILE_DIR),
                    headless=False,
                    args=["--start-maximized"],
                )
                page = await ctx.new_page()
                await page.goto(_LOGIN_OPEN_URLS[platform])
                # Wait up to 5 minutes for the URL to leave the login page
                try:
                    await page.wait_for_url(
                        lambda url: "login" not in url,
                        timeout=300_000,
                    )
                except Exception:
                    pass  # timed out — user may still be logging in
                await asyncio.sleep(3)  # let session cookies settle
                await ctx.close()
        except Exception as e:
            logger.error(f"open-login/{platform} failed: {e}")

    asyncio.create_task(_run())
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
    global pipeline_running, current_step
    import uuid as _uuid
    from job_agent.models import Application, ApplicationStatus, JobPosting, JobPlatform, TailoredResume
    from job_agent.builders.resume_builder import build_resume_docx

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

    try:
        current_step = "Auto-Apply: Loading profile..."
        await log("[APPLY] Starting auto-apply pipeline...")

        profile = agent._load_profile_cache() if not agent.profile else agent.profile
        if not profile:
            await log("[ERROR] No profile found — run Deep Rescan on the Profile tab first.", "error")
            return

        await log(f"[APPLY] Profile loaded for {profile.name}")
        current_step = "Auto-Apply: Selecting jobs..."

        # Get top jobs not yet attempted
        rows = tracker.get_jobs(min_score=min_score, limit=200)
        candidates = []
        for row in rows:
            jid = str(row["id"])
            if tracker.already_applied(jid):
                continue
            candidates.append(row)
            if len(candidates) >= max_apply:
                break

        if not candidates:
            await log("[APPLY] No new jobs to apply to — all scored jobs have already been attempted.", "complete")
            return

        await log(f"[APPLY] {len(candidates)} jobs queued for auto-apply (min score {min_score:.0f})")

        applied_count = 0
        needs_manual_count = 0
        failed_count = 0

        for i, row in enumerate(candidates):
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
                tailored = await asyncio.to_thread(
                    agent.tailor.tailor, job, profile, vault_index=agent.vault_index
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

            # Create the application record before attempting
            app_obj = Application(
                id=str(_uuid.uuid4()),
                job=job,
                resume=tailored,
                status=ApplicationStatus.QUEUED,
            )
            app_id = tracker.create_application(app_obj)
            app_obj.id = app_id

            # Attempt the auto-apply in a thread (Playwright is blocking)
            try:
                current_step = f"Submitting: {company}"
                result = await asyncio.to_thread(agent.agent.apply_one, app_obj)
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
                else:
                    failed_count += 1
                    await log(f"[APPLY] ✗ FAILED — {company}: {result.error}", "error")
                    await push_apply_update(job_id, "failed", company, title,
                                            error=result.error)

            except Exception as e:
                failed_count += 1
                logger.error(f"Apply exception for {company}: {e}")
                tracker.update_application(app_obj.id, status="failed", error=str(e))
                await log(f"[APPLY] ✗ ERROR — {company}: {e}", "error")
                await push_apply_update(job_id, "failed", company, title, error=str(e))

        summary = (f"[DONE] Auto-apply complete — "
                   f"{applied_count} applied, {needs_manual_count} need manual, "
                   f"{failed_count} failed")
        await log(summary, "complete")

    except Exception as e:
        logger.error(f"Auto-apply pipeline error: {e}")
        await log(f"[ERROR] {e}", "error")
    finally:
        pipeline_running = False
        current_step = None


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
