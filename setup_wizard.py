#!/usr/bin/env python3
"""
Job Agent — Interactive Setup Wizard
Configures vault, resume, API key, writes config.yaml, generates extension icons.
"""
import json
import os
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).parent

# ── ANSI colors (works on modern Windows too) ─────────────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        pass

CYAN = "\033[96m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
RED  = "\033[91m"; BOLD  = "\033[1m";  DIM    = "\033[2m"; RESET = "\033[0m"


def section(title):
    bar = "─" * (52 - len(title))
    print(f"\n{CYAN}{BOLD}── {title} {bar}{RESET}")

def ok(msg):    print(f"  {GREEN}✓{RESET}  {msg}")
def info(msg):  print(f"  {CYAN}→{RESET}  {msg}")
def warn(msg):  print(f"  {YELLOW}⚠{RESET}  {msg}")
def err(msg):   print(f"  {RED}✗{RESET}  {msg}")

def ask(prompt, default=None, secret=False):
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            val = getpass.getpass(f"  {BOLD}{prompt}{suffix}: {RESET}")
        else:
            val = input(f"  {BOLD}{prompt}{suffix}: {RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else (default or "")

def ask_yn(prompt, default="y"):
    ans = ask(prompt + " (y/n)", default)
    return str(ans).lower().startswith("y")


# ── PNG Icon Generator (pure stdlib, no PIL) ──────────────────────────────────

def _chunk(tag: bytes, data: bytes) -> bytes:
    c = tag + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

def _make_icon(size: int) -> bytes:
    """
    Generates a brand-colored PNG icon:
    dark navy bg (#0d1528) with a cyan-to-green radial glow in the center.
    """
    bg      = (13, 21, 40)
    accent  = (0, 212, 255)   # #00d4ff
    hi      = (0, 255, 136)   # #00ff88

    cx = cy = size / 2.0
    r_hi  = size * 0.20
    r_acc = size * 0.40

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter: None
        for x in range(size):
            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            d = (dx * dx + dy * dy) ** 0.5
            if d < r_hi:
                t = d / r_hi
                r = int(hi[0] + (accent[0] - hi[0]) * t)
                g = int(hi[1] + (accent[1] - hi[1]) * t)
                b = int(hi[2] + (accent[2] - hi[2]) * t)
            elif d < r_acc:
                t = (d - r_hi) / (r_acc - r_hi)
                r = int(accent[0] + (bg[0] - accent[0]) * t)
                g = int(accent[1] + (bg[1] - accent[1]) * t)
                b = int(accent[2] + (bg[2] - accent[2]) * t)
            else:
                r, g, b = bg
            raw.extend([r, g, b])

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )

def generate_icons():
    icons_dir = ROOT / "extension" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    for size in [16, 48, 128]:
        (icons_dir / f"icon{size}.png").write_bytes(_make_icon(size))
    return icons_dir


# ── Obsidian Vault Creator ────────────────────────────────────────────────────

VAULT_NOTES = {
    "00 — Dashboard.md": """\
# 🎯 Job Hunt Dashboard

Welcome to your **Job Agent Vault** — the AI reads every note here to build a rich
understanding of your background, then uses it to tailor your resume for each application.

## Quick Links
- [[01 — Work Experience]]
- [[02 — Skills & Tools]]
- [[03 — Education & Certs]]
- [[04 — Achievements]]
- [[05 — Target Roles]]
- [[Interview Prep/Common Questions]]

> **Tip:** Brain-dump freely — don't worry about formatting.
> The more detail you add, the better your tailored resumes will be.
""",

    "01 — Work Experience.md": """\
# 💼 Work Experience

<!-- Add your roles below. Be specific: tech used, team size, outcomes, numbers. -->
<!-- The AI extracts skills and achievements from everything you write here. -->

## [Your Most Recent Role Title] — [Company Name]
*[Start Date] → [End Date or Present]*

**What I owned:**
-

**What I built / shipped:**
-

**Impact (quantified where possible):**
-

---

## [Previous Role] — [Company]
*[Dates]*

-

""",

    "02 — Skills & Tools.md": """\
# 🛠 Skills & Tools

## Languages & Frameworks
-

## Platforms & Cloud
-

## Data & Analytics
-

## Tools & Workflow
-

## Soft Skills
-
""",

    "03 — Education & Certs.md": """\
# 🎓 Education & Certifications

## Degrees
**[Degree]** in [Field] — [University], [Year]

## Certifications
- [Cert Name] — [Issuer], [Year]

## Courses & Training
-
""",

    "04 — Achievements.md": """\
# 🏆 Achievements & Wins

<!-- Numbers sell. "Reduced load time 40%" beats "improved performance". -->

## Career Wins
-

## Awards & Recognition
-

## Projects & Side Work
-
""",

    "05 — Target Roles.md": """\
# 🎯 Target Roles

## Titles I'm Targeting
-

## Why These Roles
-

## Dream Companies
-

## Non-negotiables (what I won't do)
-

## Compensation
- Minimum total comp: $
- Target: $
- Equity matters? Y/N
""",

    "Interview Prep/Common Questions.md": """\
# 📝 Interview Prep

## Tell me about yourself
[Draft your answer here — the AI can suggest improvements based on the job]

## What's your greatest strength?


## Why are you leaving your current role?


## Where do you see yourself in 5 years?


## Questions to ask the interviewer
- What does success look like in the first 90 days?
- What's the biggest challenge the team is facing?
- How does the team handle technical debt / competing priorities?
""",
}

def create_vault(vault_path: Path):
    vault_path.mkdir(parents=True, exist_ok=True)
    # .obsidian/app.json — minimal config so Obsidian recognises the vault
    obs = vault_path / ".obsidian"
    obs.mkdir(exist_ok=True)
    (obs / "app.json").write_text(
        json.dumps({"legacyEditor": False, "livePreview": True}, indent=2),
        encoding="utf-8",
    )
    for rel, content in VAULT_NOTES.items():
        note = vault_path / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        if not note.exists():
            note.write_text(content, encoding="utf-8")
    ok(f"Vault created at: {vault_path}")


# ── Config Writer ─────────────────────────────────────────────────────────────

def write_config(vault_path, resume_path, api_key, name, email, phone="", location="", linkedin=""):
    try:
        import yaml
    except ImportError:
        err("PyYAML not installed — cannot write config.yaml.")
        return

    cfg = {
        "profile": {
            "name": name,
            "email": email,
            "phone": phone,
            "location": location,
            "linkedin_url": linkedin,
            "github_url": "",
            "website": "",
            "obsidian_vault_path": str(vault_path),
            "resume_path": str(resume_path),
        },
        "ai": {
            "anthropic_api_key": api_key,
            "model": "claude-opus-4-8",
            "resume_model": "claude-sonnet-4-6",
            "scoring_model": "claude-haiku-4-5-20251001",
            "max_tokens": 4096,
            "temperature": 0.3,
        },
        "search": {
            "keywords": ["Software Engineer", "Product Manager"],
            "locations": ["remote"],
            "min_salary": 80000,
            "max_results_per_search": 25,
            "platforms": ["indeed", "linkedin"],
            "job_types": ["fulltime"],
            "exclude_companies": [],
            "exclude_keywords": [],
        },
        "automation": {
            "headless": False,
            "slow_mo_ms": 150,
            "timeout_ms": 30000,
            "max_applications_per_run": 20,
            "pause_on_captcha": True,
            "auto_submit": False,
            "screenshot_on_apply": True,
        },
        "output": {
            "output_dir": "./output",
            "resumes_dir": "./output/resumes",
            "screenshots_dir": "./output/screenshots",
            "db_path": "./output/applications.db",
            "resume_template": "modern",
        },
    }
    config_path = ROOT / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    ok(f"Config written: {config_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"""
{CYAN}{BOLD}
  ╔══════════════════════════════════════════════════════════╗
  ║  🤖  JOB AGENT — SETUP WIZARD                          ║
  ║      Let's get your AI job hunter ready!                ║
  ╚══════════════════════════════════════════════════════════╝
{RESET}""")

    # ── Step 1: Obsidian Vault ────────────────────────────────────────────────
    section("Step 1 of 5 — Obsidian Vault")
    print(f"""
  Job Agent uses your Obsidian vault as a knowledge base.
  It mines your notes for skills and experience to write
  better-tailored resumes for each application.

  {DIM}Obsidian is free at https://obsidian.md{RESET}
""")
    default_vault = str(Path.home() / "Documents" / "JobVault")

    if ask_yn("  Create a fresh Job Vault with starter templates?", "y"):
        vault_str = ask("  Vault folder location", default_vault)
        vault_path = Path(vault_str).expanduser().resolve()
        if vault_path.exists() and any(vault_path.iterdir()):
            warn(f"Folder already has files: {vault_path}")
            if not ask_yn("  Use it anyway (skips creating notes)?", "n"):
                vault_str = ask("  Enter a different path")
                vault_path = Path(vault_str).expanduser().resolve()
                create_vault(vault_path)
        else:
            create_vault(vault_path)
        info("Open this folder in Obsidian and fill in your work history.")
    else:
        vault_str = ask("  Path to your existing Obsidian vault")
        vault_path = Path(vault_str).expanduser().resolve()
        if not vault_path.exists():
            warn("Path doesn't exist — update it later in the Config tab.")

    # ── Step 2: Resume ────────────────────────────────────────────────────────
    section("Step 2 of 5 — Resume File")
    print(f"""
  Point the agent at your resume (PDF or DOCX).
  It reads this alongside your vault for extra context.
  {DIM}Leave blank to skip — you can set it later in the Config tab.{RESET}
""")
    resume_str = ask("  Resume path (PDF or DOCX)", "")
    resume_path = Path(resume_str).expanduser().resolve() if resume_str else Path("")
    if resume_str and not resume_path.exists():
        warn("File not found — update the path later in Config tab.")

    # ── Step 3: Personal Info ─────────────────────────────────────────────────
    section("Step 3 of 5 — Basic Info")
    print()
    name     = ask("  Full name")
    email    = ask("  Email address")
    phone    = ask("  Phone (optional)", "")
    location = ask("  Location (e.g. Chicago, IL / remote)", "")
    linkedin = ask("  LinkedIn URL (optional)", "")

    # ── Step 4: Anthropic API Key ─────────────────────────────────────────────
    section("Step 4 of 5 — Anthropic API Key")
    print(f"""
  Claude AI powers job scoring, resume tailoring, and form filling.
  Get your key at: {CYAN}https://console.anthropic.com{RESET}
  {DIM}The key is stored locally in config.yaml — never shared.{RESET}
""")
    api_key = ask("  API key (sk-ant-...)", secret=True)
    if api_key and not api_key.startswith("sk-"):
        warn("Key format looks unexpected — you can update it in the Config tab.")

    # ── Write config ──────────────────────────────────────────────────────────
    section("Writing config.yaml")
    write_config(vault_path, resume_path, api_key, name, email, phone, location, linkedin)

    # ── Step 5: Chrome Extension icons ───────────────────────────────────────
    section("Step 5 of 5 — Chrome Extension")
    print(f"""
  Generating extension icons...")
""")
    try:
        icons_dir = generate_icons()
        ok(f"Icons generated: {icons_dir}")
    except Exception as e:
        warn(f"Icon generation failed (non-fatal): {e}")

    ext_path = (ROOT / "extension").resolve()
    print(f"""
  Install the Job Agent Chrome extension to:
  • See your agent status in the browser toolbar
  • Detect jobs on LinkedIn / Indeed / ZipRecruiter / Glassdoor
  • Send jobs to your queue with one click

  {CYAN}How to install:{RESET}
  1. Open Chrome → type in address bar: {BOLD}chrome://extensions{RESET}
  2. Enable {BOLD}Developer mode{RESET} (toggle in top-right corner)
  3. Click {BOLD}Load unpacked{RESET}
  4. Select this folder:
     {BOLD}{ext_path}{RESET}
""")

    # ── Done ──────────────────────────────────────────────────────────────────
    section("All done!")
    print(f"""
  {GREEN}{BOLD}Setup complete! Here's how to start:{RESET}

  {CYAN}Windows:{RESET}  Double-click  {BOLD}start_job_agent.bat{RESET}
  {CYAN}Mac/Linux:{RESET} Run           {BOLD}./start_job_agent.sh{RESET}

  Then open {BOLD}http://localhost:8000{RESET} in Chrome.

  First things to do in the web UI:
  → {CYAN}Config tab{RESET}  Set your job keywords and target locations
  → {CYAN}Config tab{RESET}  Verify LinkedIn / Indeed login (for Easy Apply)
  → {CYAN}Dashboard{RESET}   Hit {BOLD}Start Search{RESET} and watch it go
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
