"""
Job Agent Web Server - FastAPI Backend
Provides REST API and WebSocket endpoints for the React frontend
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sys
import os

# Add parent directory to path to import job_agent
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_agent.config import load_config
from job_agent.orchestrator import JobOrchestrator
from job_agent.db.tracker import ApplicationTracker

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
    id: int
    title: str
    company: str
    location: str
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    url: str
    fit_score: float
    salary_score: float
    combined_score: float
    status: str  # found, scored, applied, rejected
    date_found: str

# ── Global State ──

pipeline_running = False
current_step = None
agent = None
tracker = None
config = None
log_buffer = []

# ── Startup ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager"""
    global agent, tracker, config
    
    logger.info("Starting Job Agent Web Server...")
    try:
        config = load_config()
        agent = JobOrchestrator(config)
        tracker = ApplicationTracker(config)
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
    """Get the user's AI-synthesized profile"""
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    try:
        profile = agent.load_profile()
        return {
            "name": profile.name,
            "email": profile.email,
            "phone": profile.phone,
            "location": profile.location,
            "linkedin_url": profile.linkedin_url,
            "summary": profile.summary,
            "skills": profile.skills[:20],  # Top 20 skills
            "unique_value_props": profile.unique_value_props,
            "vault_notes_count": len(profile.raw_vault_text) // 100,  # Approximate
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
                
                cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'found'")
                jobs_found = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'scored'")
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
        db_path = Path(config.output.output_dir) / "applications.db"
        if not db_path.exists():
            return []
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = "SELECT * FROM applications"
        params = []
        
        if status:
            query += " WHERE status = ?"
            params.append(status)
        
        query += " ORDER BY combined_score DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        jobs = [
            JobResult(
                id=row["id"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                salary_min=row.get("salary_min"),
                salary_max=row.get("salary_max"),
                url=row["url"],
                fit_score=row.get("fit_score", 0),
                salary_score=row.get("salary_score", 0),
                combined_score=row.get("combined_score", 0),
                status=row["status"],
                date_found=row.get("date_found", ""),
            )
            for row in rows
        ]
        
        conn.close()
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

# ── WebSocket for Real-time Logs ──

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    
    async def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
    
    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.warning(f"Failed to send WebSocket message: {e}")

manager = ConnectionManager()

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
        manager.disconnect(websocket)

# ── Background Tasks ──

async def run_search_background(settings: Optional[SearchSettings]):
    """Run job search in background and stream logs"""
    global pipeline_running, current_step
    
    try:
        current_step = "Loading Profile..."
        await manager.broadcast(json.dumps({
            "type": "log",
            "message": "[SEARCH] Starting job search...",
            "timestamp": datetime.now().isoformat()
        }))
        
        profile = agent.load_profile()
        
        current_step = "Searching Jobs..."
        await manager.broadcast(json.dumps({
            "type": "log",
            "message": f"[PROFILE] Loaded profile for {profile.name}",
            "timestamp": datetime.now().isoformat()
        }))
        
        jobs = agent.searcher.search_all()
        
        current_step = "Scoring Jobs..."
        await manager.broadcast(json.dumps({
            "type": "log",
            "message": f"[SEARCH] Found {len(jobs)} jobs",
            "timestamp": datetime.now().isoformat()
        }))
        
        scored_jobs = agent.scorer.score_batch(jobs, profile)
        
        current_step = "Saving Results..."
        await manager.broadcast(json.dumps({
            "type": "log",
            "message": f"[SCORE] Scored {len(scored_jobs)} jobs",
            "timestamp": datetime.now().isoformat()
        }))
        
        await manager.broadcast(json.dumps({
            "type": "complete",
            "message": "Job search completed!",
            "timestamp": datetime.now().isoformat()
        }))
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        await manager.broadcast(json.dumps({
            "type": "error",
            "message": f"Error: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }))
    finally:
        pipeline_running = False
        current_step = None

# ── Static Files ──
# Serve React frontend
frontend_path = Path(__file__).parent.parent / "frontend" / "build"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
