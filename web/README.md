# Job Agent — Web UI & API

FastAPI backend + single-file frontend for the Job Agent.

See the [root README](../README.md) for full setup, feature documentation, and configuration.

---

## Quick start

```bash
# From the repo root (after running setup.bat / setup.sh once):
python web/backend/main.py
# Then open http://localhost:8000
```

Or use the launcher:
```
start_job_agent.bat   (Windows)
./start_job_agent.sh  (Mac / Linux)
```

---

## Architecture

```
web/
├── backend/
│   └── main.py          FastAPI server — REST API + WebSocket
└── frontend/
    └── index.html       All-in-one UI (HTML + CSS + vanilla JS, no build step)
```

The frontend is a single HTML file served directly by FastAPI. No npm, no bundler.

---

## API Reference

### Health & Status
```
GET  /api/health                     Server health check
GET  /api/status                     Pipeline running state + live counters
GET  /api/score                      XP, level, streak, run history (gamification)
```

### Profile
```
GET  /api/profile                    Cached AI profile (instant — no Claude call)
POST /api/rescan-profile             Force-rebuild profile from vault + resume
```

### Jobs
```
GET  /api/jobs?status=X&limit=50     All jobs from DB, optional status filter
GET  /api/jobs/{id}/brief            AI match brief — tailored summary + skills (calls Claude)
```

### Pipeline
```
POST /api/scan-jobs?min_score=60     Search + score, no apply
POST /api/start-search               Search + score + stream results to WS
POST /api/stop-pipeline              Graceful stop after current job
POST /api/auto-apply?min_score=70    Auto-apply to all qualifying queued jobs
POST /api/jobs/{id}/apply            Apply to a single job by ID
PATCH /api/jobs/{id}/status          Manually update application status
POST /api/jobs/{id}/mark-applied-manually
GET  /api/needs-manual               Jobs that failed automation, awaiting manual apply
```

### AI Generation
```
POST /api/cover-letter/{job_id}      Generate a tailored cover letter
POST /api/outreach/{job_id}          Generate full outreach kit + playbook
```

### Config
```
GET  /api/config                     Read config.yaml as JSON
POST /api/config                     Write config.yaml (deep-merges body)
```

### Platform Logins
```
GET  /api/login-status               LinkedIn + Indeed session check (reads cookie DB)
POST /api/open-login/{platform}      Open persistent browser for manual login
POST /api/open-browser               Open persistent browser to any URL
```

### CAPTCHA
```
GET  /api/captcha-status             Whether the pipeline is paused waiting for CAPTCHA solve
POST /api/captcha-solved             Resume the pipeline after manual CAPTCHA solve
```

### Chrome Extension
```
POST /api/smart-fill                 Fill fields for a job form (extension → backend)
POST /api/learn-pattern              Reinforce field semantics after submission
POST /api/learn-field                Save manually entered / corrected field values
GET  /api/known-sites                Domains where the extension has submitted
GET  /api/field-intelligence         Global field semantics stats
POST /api/track-job                  Queue a job from the extension
```

### Logs
```
GET  /api/run-log                    Markdown run log content
DELETE /api/run-log                  Clear run log
GET  /api/improvements               Self-improvement log (open issues, recent wins)
```

### WebSocket
```
WS   /ws/logs                        Real-time log stream + pipeline events
```

#### WebSocket message types (server → client)

| `type` | Payload fields | When sent |
|--------|---------------|-----------|
| `log` | `message`, `timestamp` | General pipeline log line |
| `complete` | `message`, `timestamp` | Step or run completed |
| `error` | `message`, `timestamp` | Pipeline error |
| `jobs` | `jobs[]`, `timestamp` | New batch of scored jobs ready |
| `apply_update` | `job_id`, `status`, `company`, `title`, `error`, `notes` | Single job apply result |
| `scan_complete` | `new_count`, `total_scored`, `timestamp` | Scan-only run finished |
| `score_update` | `run_score`, `xp_earned`, `total_xp`, `level`, `streak`, `best_score` | Run score broadcast |
| `captcha_detected` | `job_id`, `company`, `title`, `url` | CAPTCHA pause triggered |
| `captcha_resolved` | `timestamp` | CAPTCHA solved, pipeline resuming |

---

## Interactive API docs

```
http://localhost:8000/docs
```

Full Swagger UI with all endpoints, request/response schemas, and try-it-out.

---

## Troubleshooting

**Port already in use**
```bash
# Edit start_job_agent.bat / start_job_agent.sh to change the port, or:
uvicorn web.backend.main:app --port 8001
```

**WebSocket not connecting** — normal on first load. The frontend auto-reconnects every 3 seconds.

**Cover letter / Intel returning 500** — your profile cache may be empty. Go to the Profile tab → Deep Rescan.

**Jobs not saving** — check `output/applications.db` exists (created automatically on first run).
