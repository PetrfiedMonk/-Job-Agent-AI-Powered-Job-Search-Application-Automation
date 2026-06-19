# 🤖 Job Agent — AI-Powered Job Search & Application Automation

An autonomous AI agent that finds jobs, tailors your resume for each one, and submits applications — all while you sleep.

Built by [Justin Carano](https://seekbridge.ai), founder of SeekBridge.ai.

---

## What It Does

Most job hunters send the same resume to 50 companies and hear nothing. This agent does the opposite: it studies each job posting, rewrites your resume to mirror the exact language and keywords the employer used, scores it for ATS compatibility, and submits — automatically.

**The pipeline:**

```
Obsidian Vault + Resume
        ↓
  AI Profile Synthesis  (Claude builds a rich profile from all your knowledge)
        ↓
  Multi-Platform Search  (Indeed, LinkedIn, ZipRecruiter, Glassdoor)
        ↓
  AI Job Scoring  (fit score + salary score per posting)
        ↓
  Per-Job Resume Tailoring  (Claude rewrites your resume for each role)
        ↓
  DOCX Resume Generation  (clean, ATS-friendly Word document)
        ↓
  Playwright Auto-Apply  (fills forms, uploads resume, submits)
        ↓
  SQLite Application Tracker  (full pipeline dashboard)
```

---

## Features

- **Obsidian vault integration** — Point it at your vault and it mines your notes for skills, projects, and experience you forgot you had
- **AI resume tailoring** — Every application gets a resume written specifically for that job, mirroring the posting's language for maximum ATS score
- **Multi-platform search** — Searches Indeed, LinkedIn, ZipRecruiter, and Glassdoor simultaneously
- **Intelligent job scoring** — Scores each job 0–100 for fit and salary potential before spending an apply cycle on it
- **Browser automation** — Handles Indeed Easy Apply, LinkedIn Easy Apply, and generic ATS forms (Greenhouse, Lever, Workday, etc.)
- **Safe by default** — `auto_submit = false` means forms get filled and screenshotted but nothing is submitted until you say so
- **Full application tracker** — SQLite database tracks every job found, scored, applied to, and interviewed for

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/YOUR_USERNAME/job-agent.git
cd job-agent

pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure

Copy the example config and fill it in:

```bash
cp config.yaml.example config.yaml
```

Open `config.yaml` and set:

```yaml
profile:
  obsidian_vault_path: "/path/to/your/ObsidianVault"
  resume_path: "/path/to/your/resume.pdf"
  name: "Your Name"
  email: "you@email.com"

ai:
  anthropic_api_key: "sk-ant-..."   # or set ANTHROPIC_API_KEY env var

search:
  keywords:
    - "Product Manager"
    - "Business Analyst"
  locations:
    - "remote"
    - "Chicago IL"
  min_salary: 80000
```

### 3. Test your profile

Before running anything, verify the agent understands your background:

```bash
python -m job_agent.main test-profile
```

This shows you the AI-synthesized profile it built from your vault and resume — the foundation for every tailored resume it generates.

### 4. Search and score (no applications yet)

```bash
python -m job_agent.main search
```

Finds jobs, scores them, saves to the database. Review what it found before applying.

### 5. Run the full pipeline

```bash
# Review mode — fills forms but doesn't submit (safe to run anytime)
python -m job_agent.main run

# Live mode — actually submits applications
python -m job_agent.main run --live
```

---

## CLI Reference

```
python -m job_agent.main <command> [options]

Commands:
  setup           Write a config.yaml template
  test-profile    Build and display your AI profile
  search          Search and score jobs only (no applying)
  apply           Apply to jobs already queued in the database
  run             Full pipeline: search → score → tailor → apply
  dashboard       Show current pipeline stats

Options:
  --config, -c    Path to config file (default: config.yaml)
  --min-score, -s Minimum fit score to apply (default: 65)
  --max-apply, -n Max applications per run
  --live, -l      Enable auto_submit (actually submit applications)
```

---

## Project Structure

```
job_agent/
├── main.py              # CLI entry point
├── config.py            # Configuration management
├── models.py            # Data models
├── orchestrator.py      # Main pipeline coordinator
│
├── parsers/
│   ├── vault_parser.py  # Obsidian vault reader & categorizer
│   └── resume_parser.py # PDF/DOCX resume text extractor
│
├── ai/
│   ├── profile_builder.py  # Synthesizes vault + resume → unified profile
│   ├── job_scorer.py       # Scores job fit and salary potential
│   └── resume_tailor.py    # Generates ATS-optimized tailored resumes
│
├── builders/
│   └── resume_builder.py   # Renders TailoredResume → DOCX file
│
├── search/
│   └── job_searcher.py     # Multi-platform job search (jobspy)
│
├── automation/
│   └── application_agent.py  # Playwright form-filler & submitter
│
└── db/
    └── tracker.py          # SQLite application pipeline tracker
```

---

## How the AI Tailoring Works

For each job, the agent:

1. Reads the full job description and extracts required skills and keywords
2. Loads your complete profile (resume + vault content)
3. Asks Claude to select the 3–5 most relevant experiences from your history
4. Rewrites each experience bullet to emphasize outcomes relevant to *this specific role*
5. Mirrors the exact terminology from the job posting (if they say "roadmap," your resume says "roadmap")
6. Estimates an ATS match score before generating the final DOCX

The result is a resume that reads like it was written *for that job* — because it was.

---

## Safety & Ethics

- **`auto_submit = false` by default.** The agent fills forms and takes screenshots but does not submit until you explicitly enable it with `--live`.
- **CAPTCHA detection.** If a CAPTCHA is detected, the agent pauses and prompts you to solve it manually.
- **Rate limiting.** The agent includes polite delays between searches and applications to avoid getting blocked.
- **No credential storage.** Your API keys and passwords are never written to disk by the agent. LinkedIn sessions use your existing browser profile.
- This tool is intended for legitimate job seeking. Use responsibly and in accordance with each platform's terms of service.

---

## Requirements

- Python 3.10+
- [Anthropic API key](https://console.anthropic.com) (Claude powers the AI tailoring)
- Chromium (installed automatically via `playwright install chromium`)

---

## Configuration Reference

| Key | Description | Default |
|-----|-------------|---------|
| `profile.obsidian_vault_path` | Path to your Obsidian vault folder | — |
| `profile.resume_path` | Path to your resume (PDF or DOCX) | — |
| `search.keywords` | Job titles / keywords to search | `["Product Manager"]` |
| `search.locations` | Cities or `"remote"` | `["remote"]` |
| `search.min_salary` | Minimum salary filter | `80000` |
| `search.platforms` | Platforms to search | `["indeed", "linkedin"]` |
| `automation.auto_submit` | Actually submit applications | `false` |
| `automation.headless` | Run browser invisibly | `false` |
| `automation.max_applications_per_run` | Cap per run | `20` |
| `ai.model` | Claude model for profile building | `claude-opus-4-8` |
| `ai.resume_model` | Claude model for tailoring (faster) | `claude-sonnet-4-6` |

---

## Background

This project grew out of [SeekBridge.ai](https://seekbridge.ai), a web-based job application tool I built and launched. This is the agentic evolution — instead of a web UI, it's a fully autonomous pipeline that runs locally and applies on your behalf.

The core insight: volume matters in job hunting, but so does relevance. This agent maximizes both — it can run 20+ tailored applications in the time it takes to manually fill one form.

---

## License

MIT — use it, fork it, build on it.
