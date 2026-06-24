# Job Agent

An autonomous AI agent that finds jobs, tailors your resume for each one, and submits applications — while you do something better with your time.

Built by [Justin Carano](https://seekbridge.ai).

---

## Install in 2 minutes

### Windows
```
1. git clone https://github.com/YOUR_USERNAME/job-agent.git
2. Double-click  setup.bat
3. Follow the prompts (vault path, resume path, API key)
4. Double-click  start_job_agent.bat
5. Open http://localhost:8000 in Chrome
```

### Mac / Linux
```bash
git clone https://github.com/YOUR_USERNAME/job-agent.git
cd job-agent
chmod +x setup.sh && ./setup.sh
./start_job_agent.sh
```

Then open **http://localhost:8000** in Chrome.

---

## What setup does

`setup.bat` / `setup.sh` handles everything:

| Step | What happens |
|------|-------------|
| Python check | Fails with a download link if Python 3.10+ isn't found |
| Chrome check | Warns with a download link if Chrome isn't installed |
| `.venv` | Creates an isolated Python environment |
| `pip install` | Installs all dependencies from `requirements.txt` |
| Playwright | Downloads the Chromium browser for automation |
| Obsidian vault | Creates a `JobVault` with starter notes **OR** links to your existing vault |
| Resume | Asks for your resume path (PDF or DOCX) |
| API key | Saves your Anthropic key to `config.yaml` |
| Extension icons | Generates the Chrome extension icons |

---

## How it works

```
Obsidian Vault + Resume
        ↓
  AI Profile Synthesis      Claude reads your notes and builds a rich career profile
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

### Pipeline
- **AI profile synthesis** — reads every note in your Obsidian vault to surface skills, projects, and achievements you may have forgotten to put on your resume
- **Multi-platform scraping** — searches Indeed, LinkedIn, ZipRecruiter, and Glassdoor in one run
- **AI job scoring** — Claude scores each posting on fit (0–100) and salary likelihood (0–100), then adds a recency bonus for fresh listings
- **Country filter** — configure `allowed_countries` to drop postings from countries you can't work in
- **Min score threshold** — set `min_score_to_apply` so the bot only submits to jobs that actually match
- **Per-job resume tailoring** — every application gets a resume written for that exact role, mirroring the posting's language for maximum ATS pass-through
- **DOCX generation** — clean, recruiter-ready Word document output per application
- **Vault resume notes** — each tailored resume is saved to your Obsidian vault (`Job Applications/Resumes/`) with keywords matched, highlighted skills, and ATS score estimate
- **CAPTCHA handling** — agent pauses and shows a "boss fight" alert when it hits a CAPTCHA; you solve it manually, it resumes automatically
- **Persistent browser sessions** — log in once on the login tab; sessions are stored locally and reused

### Web Dashboard (`http://localhost:8000`)

| Tab | What it does |
|-----|-------------|
| Dashboard | Live metrics (jobs found / scored / applied), XP / level / streak, launch controls |
| Profile | AI-synthesized profile from vault + resume; Deep Rescan button |
| Jobs | All discovered jobs with rarity tier, fit score, salary, per-card Apply / Cover Letter / Intel |
| Tracker | Full application history; expandable inline panel shows the tailored resume used for each application |
| Auto Apply | Queue jobs by score threshold; live progress; CAPTCHA alert & resume button |
| Intel | Per-job outreach playbook — LinkedIn message, cold email, follow-up, 5-tactic hacker playbook |
| Config | Keywords, locations, country filter, score threshold, platform logins, API key |
| Logs | Live pipeline log stream via WebSocket |

### Gamification
- XP earned per application, manual apply, and streak bonus
- Level system with progressive thresholds
- Rarity tiers: COMMON → UNCOMMON → RARE → EPIC → LEGENDARY (based on fit score)
- Combo counter for consecutive successful runs
- Radar scan animation during active pipeline

### Cover Letters
Generate a Claude-powered cover letter for any job from the Jobs tab — tailored to the exact role and company.

### Intel / Outreach
Generate a full outreach kit for any job from the Jobs tab:
- LinkedIn connection request (under 300 chars)
- LinkedIn DM (post-connection follow-up)
- Cold email with subject line
- Follow-up email
- 5-tactic hacker playbook with specific, channel-by-channel actions

### Chrome Extension
Lives in `extension/`. Shows agent status in the toolbar and sends jobs to your queue from any listing page with one click.

---

## Requirements

- **Python 3.10+** — [python.org](https://www.python.org/downloads/)
- **Google Chrome** — [google.com/chrome](https://www.google.com/chrome/)
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com)
- **Obsidian** (optional, strongly recommended) — [obsidian.md](https://obsidian.md)

---

## Configuration

All settings live in `config.yaml` and are also editable in the Config tab of the web UI.

### Profile
| Key | Description |
|-----|-------------|
| `profile.obsidian_vault_path` | Path to your Obsidian vault folder |
| `profile.resume_path` | Path to your resume (PDF or DOCX) |
| `profile.name` / `email` / `phone` | Auto-filled into job application forms |
| `profile.address_line1` / `city` / `state` / `zip_code` | Used for location fields on application forms |
| `profile.work_authorization` | e.g. `"US Citizen"`, `"H1B"` |
| `profile.linkedin_url` / `github_url` / `website` | Filled into profile fields |

### Search
| Key | Description | Default |
|-----|-------------|---------|
| `search.keywords` | Job titles to search (one per entry) | `["Product Manager"]` |
| `search.locations` | Cities or `"remote"` | `["remote"]` |
| `search.platforms` | `["indeed", "linkedin", "ziprecruiter", "glassdoor"]` | `["indeed", "linkedin"]` |
| `search.max_results_per_search` | Listings fetched per keyword+location pair | `25` |
| `search.min_salary` | Minimum salary filter passed to job boards | `80000` |
| `search.job_types` | `["fulltime", "parttime", "contract", "internship"]` | `["fulltime"]` |
| `search.exclude_companies` | Companies to skip entirely | `[]` |
| `search.exclude_keywords` | Job titles containing these are dropped | `[]` |
| `search.allowed_countries` | Only keep jobs whose location matches these countries. Empty = allow all. | `["United States", "Remote"]` |

### Automation
| Key | Description | Default |
|-----|-------------|---------|
| `automation.auto_submit` | Actually submit applications (set `true` when ready) | `false` |
| `automation.headless` | Run browser invisibly | `false` |
| `automation.max_applications_per_run` | Cap on applications per pipeline run | `20` |
| `automation.min_score_to_apply` | Combined score (0–100) required before auto-applying | `70` |
| `automation.pause_on_captcha` | Pause and alert on CAPTCHA detection | `true` |
| `automation.screenshot_on_apply` | Save a screenshot after each apply attempt | `true` |

### AI
| Key | Description | Default |
|-----|-------------|---------|
| `ai.anthropic_api_key` | Your Anthropic key (or set `ANTHROPIC_API_KEY` env var) | `""` |
| `ai.model` | Model used for profile building and cover letters | `claude-opus-4-8` |
| `ai.resume_model` | Model used for resume tailoring | `claude-sonnet-4-6` |
| `ai.scoring_model` | Model used for batch job scoring (cost-optimized) | `claude-haiku-4-5-20251001` |

---

## Obsidian Integration

The agent uses your vault as a knowledge base and writes back to it after every run.

**Reads:**
- All notes — skills, projects, achievements, anything you've written about your career
- `Job Agent - Learned Answers.md` — answers you've manually entered on past applications, fed back in on the next run

**Writes:**
- `Job Applications/Resumes/YYYY-MM-DD_Company_Title.md` — tailored resume note per application, with keywords matched, highlighted skills, and ATS score
- `Job Applications/Sessions/YYYY-MM-DD HH-MM Run.md` — session summary after each auto-apply run
- `Job Agent - Learned Answers.md` — updated whenever you manually fill or correct a form field via the Chrome extension

The more detail you put in your vault, the better the AI can tailor your resume.

---

## Chrome Extension

Install once:
1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder

What it does:
- Shows running / idle / offline badge in the toolbar
- Detects job listings on LinkedIn, Indeed, ZipRecruiter, Glassdoor
- "Send to Job Agent" queues the current job with one click
- Smart form fill — fills application forms automatically using your profile and learned answers
- Links back to the web dashboard

---

## Safety

- **`auto_submit: false` by default.** Forms are filled and screenshotted; nothing submits until you set this to `true`.
- **CAPTCHA pausing.** The agent stops and alerts you when it hits a CAPTCHA; you solve it, it continues.
- **Rate limiting.** Polite delays between searches and apply actions.
- **Local only.** Config, output, browser sessions, and all data stay on your machine. Nothing is sent anywhere except the Anthropic API for AI calls.
- **Use responsibly.** This tool is for legitimate job seeking. Comply with each platform's terms of service.

---

## Project Structure

```
job-agent/
├── setup.bat / setup.sh         Windows and Mac/Linux installers
├── start_job_agent.bat / .sh    Launchers
├── config.yaml                  Your settings (created by setup)
├── requirements.txt             Python dependencies
│
├── extension/                   Chrome extension
│   ├── manifest.json
│   ├── popup.html / popup.js
│   ├── background.js
│   ├── content.js / filler.js
│   └── icons/
│
├── job_agent/                   Core pipeline
│   ├── config.py                Config dataclasses + YAML loader
│   ├── orchestrator.py          Top-level pipeline coordinator
│   ├── models.py                JobPosting, UserProfile, Application, etc.
│   ├── parsers/
│   │   ├── vault_parser.py      Obsidian vault → profile data
│   │   └── resume_parser.py     PDF/DOCX resume → profile data
│   ├── ai/
│   │   ├── profile_builder.py   Claude: vault + resume → UserProfile
│   │   ├── job_scorer.py        Claude: batch scoring + country filter
│   │   ├── resume_tailor.py     Claude: per-job resume tailoring
│   │   └── form_filler.py       Claude: application form answers
│   ├── search/
│   │   └── job_searcher.py      Multi-platform scraping via jobspy
│   ├── automation/
│   │   ├── application_agent.py Playwright: navigate + fill + submit
│   │   └── ats_handlers.py      Platform-specific ATS logic
│   ├── builders/
│   │   └── resume_builder.py    Tailored resume → DOCX
│   └── db/
│       ├── tracker.py           SQLite: jobs, applications, scores
│       ├── run_log.py           Markdown run log writer
│       └── field_semantics.py   Global form field learning DB
│
└── web/
    ├── backend/main.py          FastAPI server (REST + WebSocket)
    └── frontend/index.html      All-in-one web UI
```

---

## Background

This project grew out of [SeekBridge.ai](https://seekbridge.ai) — a job application tool I built and launched. This is the agentic evolution: a fully autonomous pipeline that runs locally and applies on your behalf.

The core insight: volume matters in job hunting, but so does relevance. This agent maximizes both.

---

## License

MIT — use it, fork it, build on it.
