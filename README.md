# 🤖 Job Agent

An autonomous AI agent that finds jobs, tailors your resume for each one, and submits applications — while you do something better with your time.

Built by [Justin Carano](https://seekbridge.ai).

---

## Install in 2 minutes

### Windows
```
1. git clone https://github.com/YOUR_USERNAME/job-agent.git
2. Double-click  setup.bat
3. Follow the prompts (vault, resume, API key)
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

`setup.bat` / `setup.sh` handles everything for a fresh install:

| Step | What happens |
|------|-------------|
| Python check | Fails with a download link if Python 3.10+ isn't found |
| Chrome check | Warns with a download link if Chrome isn't installed |
| `.venv` | Creates an isolated Python environment |
| `pip install` | Installs all dependencies from `requirements.txt` |
| Playwright | Downloads the Chromium browser for automation |
| Obsidian vault | Creates a new Job Vault with starter notes **OR** lets you point to an existing one |
| Resume | Asks for your resume path (PDF or DOCX) |
| API key | Saves your Anthropic key to `config.yaml` |
| Extension icons | Generates the Chrome extension icons |

---

## Chrome Extension

The **Job Agent extension** lives in the `extension/` folder. It shows your agent status in the browser toolbar and lets you send job postings to your queue from any listing page.

**Install (one time):**
1. Open Chrome → go to `chrome://extensions`
2. Enable **Developer mode** (toggle, top-right)
3. Click **Load unpacked** → select the `extension/` folder

**What it does:**
- Shows running / idle / offline status in the toolbar badge
- Detects job postings on LinkedIn, Indeed, ZipRecruiter, Glassdoor
- "Send to Job Agent" button queues the current job with one click
- Links directly to the web dashboard

> Run `setup.bat` or `setup.sh` first — it generates the icon files the extension needs.

---

## Obsidian Vault

The agent uses your Obsidian vault as a knowledge base. It reads every note to build a rich understanding of your background, then uses that context to tailor your resume for each job.

**New vault (recommended for new users):** Setup creates a `JobVault` folder with starter templates:
- Work Experience
- Skills & Tools
- Education & Certs
- Achievements
- Target Roles
- Interview Prep

Fill in as much or as little as you want — the more detail, the better the AI can tailor your resume.

**Existing vault:** Just point setup at your current vault folder.

> Obsidian is free at [obsidian.md](https://obsidian.md)

---

## How it works

```
Obsidian Vault + Resume
        ↓
  AI Profile Synthesis   (Claude builds a rich career profile from your notes)
        ↓
  Multi-Platform Search  (Indeed, LinkedIn, ZipRecruiter, Glassdoor)
        ↓
  AI Job Scoring         (0–100 fit score + salary score per posting)
        ↓
  Per-Job Resume Tailoring  (Claude rewrites your resume for each role)
        ↓
  DOCX Resume Generation    (clean, ATS-ready Word document)
        ↓
  Playwright Auto-Apply  (fills forms, handles CAPTCHAs, submits)
        ↓
  SQLite Application Tracker
```

---

## Features

- **Gamified UI** — rarity tiers (LEGENDARY / EPIC / RARE), XP system, combo counter, radar scan animations, particle bursts. Job hunting feels like winning, not paperwork.
- **Obsidian integration** — mines your notes for skills, projects, and experience you forgot you had
- **AI resume tailoring** — every application gets a resume written for that specific job, mirroring the posting's exact language for maximum ATS score
- **CAPTCHA handling** — when a CAPTCHA appears, the agent pauses and alerts you (boss fight mode) so you can solve it manually, then resumes automatically
- **Platform login verification** — the Settings tab checks whether you're logged into LinkedIn and Indeed and opens a login window if not
- **Safe by default** — `auto_submit: false` means forms get filled and screenshotted but nothing is submitted until you say so
- **Chrome extension** — see agent status and queue jobs from any job listing page

---

## Requirements

- **Python 3.10+** — [python.org](https://www.python.org/downloads/)
- **Google Chrome** — [google.com/chrome](https://www.google.com/chrome/)
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com) (Claude powers the AI)
- **Obsidian** (optional but recommended) — [obsidian.md](https://obsidian.md)

`setup.bat` checks for all of these and tells you what's missing.

---

## Web UI

The web dashboard runs at `http://localhost:8000` after you start the server.

| Tab | What it shows |
|-----|--------------|
| Dashboard | Pipeline status, live metrics, Start/Stop controls |
| Profile | Your AI-synthesized profile from vault + resume |
| Jobs | All discovered jobs with rarity tier, fit score, salary |
| Tracker | Full application history |
| Auto Apply | Jobs queued for submission; CAPTCHA alerts |
| Config | Job keywords, locations, platforms; platform login status; API key |
| Logs | Live pipeline log stream |

---

## Configuration

All settings live in `config.yaml` (created by setup) or are editable in the Config tab of the web UI.

| Key | Description | Default |
|-----|-------------|---------|
| `profile.obsidian_vault_path` | Path to your Obsidian vault | — |
| `profile.resume_path` | Path to your resume (PDF or DOCX) | — |
| `search.keywords` | Job titles to search | `["Product Manager"]` |
| `search.locations` | Cities or `"remote"` | `["remote"]` |
| `search.min_salary` | Minimum salary filter | `80000` |
| `search.platforms` | Platforms to search | `["indeed", "linkedin"]` |
| `automation.auto_submit` | Actually submit applications | `false` |
| `automation.headless` | Run browser invisibly | `false` |
| `automation.max_applications_per_run` | Cap per run | `20` |
| `ai.anthropic_api_key` | Your Anthropic key (or set env var) | `""` |
| `ai.model` | Claude model for profile building | `claude-opus-4-8` |
| `ai.resume_model` | Claude model for tailoring | `claude-sonnet-4-6` |

See `config.yaml.example` for the full reference with comments.

---

## Safety & Ethics

- **`auto_submit: false` by default.** Nothing is submitted until you explicitly enable it.
- **CAPTCHA detection.** The agent pauses and alerts you; you solve it, it continues.
- **Rate limiting.** Polite delays between searches and applications.
- **Persistent sessions.** Login cookies are stored in `output/browser_profile/` — never transmitted anywhere.
- **Local-only.** Config, output, and sessions stay on your machine.
- This tool is for legitimate job seeking. Use it responsibly and in accordance with each platform's terms of service.

---

## Project Structure

```
job-agent/
├── setup.bat              ← Windows installer (run this first)
├── setup.sh               ← Mac/Linux installer
├── setup_wizard.py        ← Interactive setup script (called by setup.bat/sh)
├── start_job_agent.bat    ← Windows launcher
├── start_job_agent.sh     ← Mac/Linux launcher
├── config.yaml.example    ← Config reference
├── requirements.txt       ← Python dependencies
│
├── extension/             ← Chrome extension (load unpacked in chrome://extensions)
│   ├── manifest.json
│   ├── popup.html / popup.js
│   ├── background.js
│   ├── content.js
│   └── icons/             ← Generated by setup
│
├── job_agent/             ← Core pipeline
│   ├── config.py
│   ├── orchestrator.py
│   ├── parsers/           ← vault_parser, resume_parser
│   ├── ai/                ← profile_builder, job_scorer, resume_tailor
│   ├── search/            ← job_searcher (jobspy)
│   ├── automation/        ← application_agent (Playwright)
│   └── db/                ← tracker (SQLite)
│
└── web/
    ├── backend/main.py    ← FastAPI server
    └── frontend/index.html ← All-in-one web UI
```

---

## Background

This project grew out of [SeekBridge.ai](https://seekbridge.ai) — a job application tool I built and launched. This is the agentic evolution: a fully autonomous pipeline that runs locally and applies on your behalf.

The core insight: volume matters in job hunting, but so does relevance. This agent maximizes both.

---

## License

MIT — use it, fork it, build on it.
