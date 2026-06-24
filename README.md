# Job Agent

An autonomous AI agent that finds jobs, scores them against your profile, tailors your resume for each one, and submits applications — while you do something better with your time.

Built by [Justin Carano](https://seekbridge.ai).

---

## Install in 2 minutes

### Windows
```
1. git clone https://github.com/PetrfiedMonk/-Job-Agent-AI-Powered-Job-Search-Application-Automation.git
2. Double-click  setup.bat
3. Follow the prompts (vault path, resume path, API key)
4. Double-click  start_job_agent.bat
5. Open http://localhost:8000 in Chrome
6. Load the Chrome extension (see below)
```

### Mac / Linux
```bash
git clone https://github.com/PetrfiedMonk/-Job-Agent-AI-Powered-Job-Search-Application-Automation.git
cd "-Job-Agent-AI-Powered-Job-Search-Application-Automation"
chmod +x setup.sh && ./setup.sh
./start_job_agent.sh
```

Then open **http://localhost:8000** in Chrome.

> **Config:** Copy `config.example.yaml` → `config.yaml` and fill in your details. The setup wizard does this automatically.

---

## How it works

```
Obsidian Vault + Resume
        ↓
  AI Profile Synthesis      Claude reads your notes, surfaces hidden skills and achievements
        ↓
  Multi-Platform Search     Indeed, LinkedIn, ZipRecruiter, Glassdoor
        ↓
  AI Job Scoring            0–100 combined fit + salary score per posting
        ↓
  Country & Score Filter    Drop foreign postings; skip jobs below your threshold
        ↓
  Per-Job Resume Tailoring  Claude rewrites your resume for each specific role
        ↓
  DOCX + Vault Note         ATS-ready Word doc + Obsidian note with keywords & score
        ↓
  Playwright Auto-Apply     Fills forms, handles CAPTCHAs, submits
        ↓
  SQLite Application Tracker
```

---

## Features

### Core Pipeline
- **AI profile synthesis** — reads every note in your Obsidian vault to surface skills, projects, and achievements you may have forgotten to put on your resume
- **Multi-platform scraping** — searches Indeed, LinkedIn, ZipRecruiter, and Glassdoor in one run
- **AI job scoring** — Claude scores each posting on fit (0–100) and salary likelihood (0–100), plus a recency bonus for fresh listings
- **Country filter** — configure `allowed_countries` to drop postings from countries you can't work in
- **Min score threshold** — set `min_score_to_apply` so the bot only submits to jobs that actually match
- **Per-job resume tailoring** — every application gets a resume written for that exact role, mirroring the posting's language for maximum ATS pass-through
- **DOCX generation** — clean, recruiter-ready Word document output per application
- **Vault resume notes** — each tailored resume is saved to your Obsidian vault with keywords matched, highlighted skills, and ATS score estimate
- **CAPTCHA handling** — agent pauses and alerts you when it hits a CAPTCHA; you solve it manually, it resumes automatically
- **Persistent browser sessions** — log in once; sessions are stored locally and reused

### Web Dashboard (`http://localhost:8000`)

| Tab | What it does |
|-----|-------------|
| Dashboard | Live metrics (jobs found / applied), XP / level / streak, next-action card, launch controls |
| Profile | AI-synthesized profile from vault + resume; Deep Rescan button |
| Jobs | All discovered jobs with rarity tier, fit score, salary, per-card Auto Apply / Apply Kit / Cover Letter / Intel |
| Tracker | Full application history with tailored resume inline per application |
| War Room | Run log, performance score, improvement tracker |
| Settings | Keywords, locations, platforms, score threshold, profile info, API key |

### Manual Apply Kit
When auto-apply can't reach a job (LinkedIn, Indeed bot detection), the agent routes to Manual mode automatically:
- **One-click resume generation** — tailored DOCX for the specific role, download instantly
- **Cover letter** — Claude-written, company-specific, copy to clipboard
- **Skills checklist** — top keywords extracted from the JD so you know what to emphasize
- **Application Intel** — hiring insights, recommended outreach angle, salary read

### Gamification
- XP earned per application, manual apply, cover letter generated, and streak
- Level system with progressive thresholds
- Rarity tiers: COMMON → UNCOMMON → RARE → EPIC → LEGENDARY (based on fit score)
- Run performance grades (S / A / B / C / D / F) across four dimensions: automation rate, reach, quality, velocity

### Cover Letters & Outreach Intel
- **Cover letter** — Claude-powered, tailored to the exact role and company, one click from any job card
- **Outreach kit** — LinkedIn connection request, DM follow-up, cold email with subject line, follow-up email, 5-tactic hacker playbook

---

## Chrome Extension

The extension lives in `web/extension/`. It has two distinct modes depending on what page you're on.

### Install
1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `web/extension/` folder
4. Pin the ⚡ icon to your toolbar

### Fit Radar — Job Listing Mode
On any job search results page (LinkedIn Jobs, Indeed, Glassdoor, ZipRecruiter), the extension automatically:
- Scans all visible job cards
- Scores each one against your keywords and skills (no AI call — instant)
- Injects a score badge directly onto each card: **🔥 Hot** / **✓ Fit** / **~ Weak** / **✗ Skip**
- Adds a **+ Pipeline** button to any card — one click saves it to your dashboard for follow-up
- Re-scans automatically as you scroll (infinite scroll / SPA-aware via MutationObserver)

### Smart Fill — Application Form Mode
On any job application page (Greenhouse, Lever, Workday, company portals, etc.):
- Auto-detects the page and opens the panel
- **Smart Fill** classifies every field using global learning + fills them from your profile
- Questions (open-ended) are shown in the panel for review before filling
- AI-written answers can be edited inline before being filled into the form
- Edited answers are saved to the **Answer Playbook** and reused on future applications

### Answer Playbook
- Every question answer you edit in the extension panel is saved automatically
- Answers are keyed by question type (`why_us`, `greatest_strength`, etc.) and optionally by company
- User-saved answers always take priority over AI-generated ones on future fills
- Manage your playbook via `GET/POST/DELETE /api/playbook`

### Auto-Submission Logging
- When you submit an application form with the extension open, it automatically logs the application to your dashboard pipeline
- Works on real form submits AND SPA submit buttons (Apply Now, Submit Application, etc.)
- If the job was already in your pipeline it updates the status; otherwise it creates a new entry
- Clicking **✓ Submitted — Save What Worked** also triggers logging as a fallback

### Field Learning
- Every fill is reinforced globally — the system gets faster and more accurate across all users over time
- Corrections (fields you change after fill) are recorded so the same mistake doesn't happen again
- Learning is visible in your Obsidian vault at `Job Agent - Learned Answers.md`

---

## Requirements

- **Python 3.10+** — [python.org](https://www.python.org/downloads/)
- **Google Chrome** — [google.com/chrome](https://www.google.com/chrome/)
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com)
- **Obsidian** (optional but strongly recommended) — [obsidian.md](https://obsidian.md)

---

## Configuration

Copy `config.example.yaml` → `config.yaml` and fill in your details. All settings are also editable live in the Settings tab of the web UI.

### Profile
| Key | Description |
|-----|-------------|
| `profile.name` / `email` / `phone` | Auto-filled into every job application form |
| `profile.address_line1` / `city` / `state` / `zip_code` | Used for location fields |
| `profile.work_authorization` | e.g. `"US Citizen"`, `"H1B"` |
| `profile.linkedin_url` / `github_url` / `website` | Filled into profile fields |
| `profile.resume_path` | Path to your resume (PDF or DOCX) |
| `profile.obsidian_vault_path` | Path to your Obsidian vault folder |

### Search
| Key | Description | Default |
|-----|-------------|---------|
| `search.keywords` | Job titles to search (one per line) | `["Product Manager"]` |
| `search.locations` | Cities or `"remote"` | `["remote"]` |
| `search.platforms` | `["indeed", "linkedin", "ziprecruiter", "glassdoor"]` | `["indeed", "linkedin"]` |
| `search.max_results_per_search` | Listings fetched per keyword+location pair | `25` |
| `search.min_salary` | Minimum salary filter passed to job boards | `80000` |
| `search.exclude_companies` | Companies to skip entirely | `[]` |
| `search.exclude_keywords` | Job titles containing these are dropped | `[]` |
| `search.allowed_countries` | Only keep jobs whose location matches. Empty = allow all. | `["United States"]` |

### Automation
| Key | Description | Default |
|-----|-------------|---------|
| `automation.auto_submit` | Actually submit applications (set `true` when ready) | `false` |
| `automation.headless` | Run browser invisibly | `false` |
| `automation.max_applications_per_run` | Cap on applications per pipeline run | `20` |
| `automation.min_score_to_apply` | Combined score (0–100) required before auto-applying | `70` |
| `automation.manual_only_platforms` | Platforms routed to Manual Apply Kit (bot detection) | `["indeed", "linkedin"]` |
| `automation.pause_on_captcha` | Pause and alert on CAPTCHA detection | `true` |

### AI
| Key | Description | Default |
|-----|-------------|---------|
| `ai.anthropic_api_key` | Your Anthropic key (or set `ANTHROPIC_API_KEY` env var) | `""` |
| `ai.model` | Model for profile building, scoring, cover letters | `claude-opus-4-8` |
| `ai.resume_model` | Model for resume tailoring (cost-optimized) | `claude-sonnet-4-6` |

---

## Obsidian Integration

The agent uses your vault as a knowledge base and writes back after every run.

**Reads:**
- All notes — skills, projects, achievements, anything you've written about your career
- `Job Agent - Learned Answers.md` — answers you've manually entered on past applications

**Writes:**
- `Job Applications/Resumes/YYYY-MM-DD_Company_Title.md` — tailored resume per application
- `Job Applications/Sessions/YYYY-MM-DD HH-MM Run.md` — session summary after each run
- `Job Agent - Learned Answers.md` — updated whenever you manually fill or correct a field via the extension

The more detail you put in your vault, the better the AI tailors your resume. Work history, side projects, metrics, anything.

---

## Safety

- **`auto_submit: false` by default.** Forms are filled and screenshotted; nothing submits until you flip this flag.
- **CAPTCHA pausing.** The agent stops and alerts you when it hits a CAPTCHA.
- **Local only.** Config, database, browser sessions, and all personal data stay on your machine. Nothing is sent anywhere except the Anthropic API for AI calls.
- **Indeed & LinkedIn** are always routed to Manual Apply Kit — bot detection on these platforms means automation is unreliable; the kit gives you everything you need to apply manually in under 2 minutes.
- **Use responsibly.** This tool is for legitimate job seeking. Comply with each platform's terms of service.

---

## Project Structure

```
job-agent/
├── setup.bat / setup.sh         Windows and Mac/Linux installers
├── start_job_agent.bat / .sh    Launchers
├── config.example.yaml          Template — copy to config.yaml and fill in
├── config.yaml                  Your settings (gitignored — never committed)
├── requirements.txt             Core Python dependencies
│
├── web/
│   ├── backend/main.py          FastAPI server — REST API + WebSocket log stream
│   ├── frontend/index.html      All-in-one web UI (no build step)
│   ├── requirements.txt         Web server dependencies (FastAPI, uvicorn)
│   └── extension/               Chrome extension (load this folder in Chrome)
│       ├── manifest.json        MV3 manifest
│       ├── background.js        Service worker — relays API calls to localhost
│       ├── content.js           Page script — Fit Radar + Smart Fill + auto-log
│       ├── popup.html / .js     Extension toolbar popup
│       └── icon.png
│
├── job_agent/                   Core pipeline
│   ├── config.py                Config dataclasses + YAML loader
│   ├── orchestrator.py          Top-level pipeline coordinator
│   ├── models.py                JobPosting, UserProfile, Application dataclasses
│   ├── ai/
│   │   ├── profile_builder.py   Claude: vault + resume → UserProfile
│   │   ├── job_scorer.py        Claude: batch job scoring + country filter
│   │   ├── resume_tailor.py     Claude: per-job resume tailoring
│   │   ├── form_filler.py       Smart Fill: classify + answer form fields
│   │   └── vault_recommender.py Obsidian vault note recommendations per job
│   ├── search/
│   │   └── job_searcher.py      Multi-platform scraping via python-jobspy
│   ├── automation/
│   │   ├── application_agent.py Playwright: navigate + fill + submit
│   │   └── ats_handlers.py      Platform-specific ATS quirk handling
│   ├── builders/
│   │   └── resume_builder.py    Tailored resume → DOCX
│   └── db/
│       ├── tracker.py           SQLite: jobs, applications, run scores
│       ├── run_log.py           Markdown run log writer
│       ├── field_semantics.py   Global form field learning + Answer Playbook
│       └── improvement_tracker.py Self-improvement log
│
└── extension/                   Legacy extension scaffold (see web/extension/)
```

---

## API Reference (extension / integrations)

The backend runs on `http://localhost:8000`. Key endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/jobs` | All jobs with scores and application status |
| `POST` | `/api/jobs/score-preview` | Batch fast scoring (no AI) — used by Fit Radar |
| `POST` | `/api/jobs/add-to-pipeline` | Save a discovered job from the extension |
| `POST` | `/api/jobs/{id}/apply` | Trigger auto-apply for one job |
| `POST` | `/api/smart-fill` | Classify + fill form fields (extension Smart Fill) |
| `POST` | `/api/learn-pattern` | Reinforce field type mappings after submission |
| `POST` | `/api/extension/log-application` | Log a submitted application from the extension |
| `GET` | `/api/playbook` | List all Answer Playbook entries |
| `POST` | `/api/playbook` | Save a user-edited question answer |
| `DELETE` | `/api/playbook/{id}` | Remove a playbook entry |
| `GET` | `/api/profile` | Current AI-synthesized profile |
| `GET` | `/api/config` | Current config |
| `POST` | `/api/config` | Update config (persists to config.yaml) |
| `WS` | `/ws/logs` | Live pipeline log stream |

---

## Background

This project grew out of [SeekBridge.ai](https://seekbridge.ai) — a job application tool I built and launched. This is the agentic evolution: a fully autonomous pipeline that runs locally, learns from your behavior, and applies on your behalf.

The core insight: volume matters in job hunting, but so does relevance. This agent maximizes both — and the more you use it, the smarter it gets.

---

## License

MIT — use it, fork it, build on it.
