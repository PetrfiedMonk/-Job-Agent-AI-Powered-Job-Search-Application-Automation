"""
Configuration management for the Job Agent.
Edit config.yaml to customize behavior.
"""
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional


CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"


@dataclass
class SearchConfig:
    keywords: List[str] = field(default_factory=lambda: [
        "Product Manager", "Business Analyst", "Product Consultant",
        "Technical Product Manager", "IT Product Manager"
    ])
    locations: List[str] = field(default_factory=lambda: ["remote", "Toledo OH"])
    platforms: List[str] = field(default_factory=lambda: ["indeed", "linkedin", "ziprecruiter", "glassdoor"])
    max_results_per_search: int = 25
    min_salary: int = 80000
    job_types: List[str] = field(default_factory=lambda: ["fulltime"])
    exclude_companies: List[str] = field(default_factory=list)
    exclude_keywords: List[str] = field(default_factory=lambda: ["senior director", "VP of", "C-level"])
    # Country restriction — jobs whose location clearly matches a foreign country are dropped.
    # Empty list = no restriction. Common values: ["United States", "Canada", "Remote"]
    allowed_countries: List[str] = field(default_factory=lambda: ["United States", "Remote"])


@dataclass
class AIConfig:
    anthropic_api_key: str = ""
    model: str = "claude-opus-4-8"
    resume_model: str = "claude-sonnet-4-6"
    scoring_model: str = "claude-haiku-4-5-20251001"  # batch scoring only — 60% cheaper
    max_tokens: int = 4096
    temperature: float = 0.3


@dataclass
class AutomationConfig:
    headless: bool = False          # Show browser window (set True for background)
    slow_mo_ms: int = 150           # Delay between actions (human-like)
    timeout_ms: int = 30000
    max_applications_per_run: int = 20
    pause_on_captcha: bool = True   # Stop and alert user when CAPTCHA detected
    auto_submit: bool = False       # Safety: set True to actually submit (default: review mode)
    screenshot_on_apply: bool = True
    min_score_to_apply: int = 70    # Minimum combined score required to auto-apply
    captcha_timeout_seconds: int = 300  # Seconds to wait for human CAPTCHA solve before giving up
    auto_cover_letter: bool = False  # Generate and attach a tailored cover letter when the form has a cover letter field
    manual_only_platforms: List[str] = field(default_factory=list)  # Platforms scanned but never auto-applied (e.g. ["indeed"])


@dataclass
class OutputConfig:
    output_dir: str = "./output"
    resumes_dir: str = "./output/resumes"
    screenshots_dir: str = "./output/screenshots"
    db_path: str = "./output/applications.db"
    resume_template: str = "modern"   # modern, classic, minimal


@dataclass
class ProfileConfig:
    obsidian_vault_path: str = ""      # Path to your Obsidian vault folder
    resume_path: str = ""              # Path to your existing resume (PDF or DOCX)
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""                 # "City, ST" display string
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = "United States"
    work_authorization: str = "US Citizen"
    linkedin_url: str = ""
    github_url: str = ""
    website: str = ""


@dataclass
class AppConfig:
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load config from YAML file, with env var overrides."""
    path = Path(config_path) if config_path else CONFIG_FILE

    raw = {}
    if path.exists():
        with open(path, encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}

    cfg = AppConfig(
        profile=ProfileConfig(**raw.get("profile", {})),
        search=SearchConfig(**raw.get("search", {})),
        ai=AIConfig(**raw.get("ai", {})),
        automation=AutomationConfig(**raw.get("automation", {})),
        output=OutputConfig(**raw.get("output", {})),
    )

    # Env var overrides (great for CI / secrets management)
    if not cfg.ai.anthropic_api_key:
        cfg.ai.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")

    return cfg


def save_example_config(path: str = "config.yaml"):
    """Write a config.yaml template the user can fill in."""
    example = {
        "profile": {
            "obsidian_vault_path": "/path/to/your/obsidian/vault",
            "resume_path": "/path/to/your/resume.pdf",
            "name": "Your Name",
            "email": "your@email.com",
            "phone": "555-555-5555",
            "location": "City, State",
            "linkedin_url": "https://linkedin.com/in/yourprofile",
            "github_url": "",
            "website": "",
        },
        "search": {
            "keywords": ["Product Manager", "Business Analyst", "Technical Product Manager"],
            "locations": ["remote", "Toledo OH"],
            "platforms": ["indeed", "linkedin", "ziprecruiter", "glassdoor"],
            "max_results_per_search": 25,
            "min_salary": 80000,
            "job_types": ["fulltime"],
            "exclude_companies": [],
            "exclude_keywords": [],
        },
        "ai": {
            "anthropic_api_key": "",   # Or set ANTHROPIC_API_KEY env var
            "model": "claude-opus-4-8",
            "resume_model": "claude-sonnet-4-6",
            "scoring_model": "claude-haiku-4-5-20251001",
        },
        "automation": {
            "headless": False,
            "slow_mo_ms": 150,
            "max_applications_per_run": 20,
            "pause_on_captcha": True,
            "auto_submit": False,       # IMPORTANT: set True only when ready to actually apply
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
    with open(path, "w") as f:
        yaml.dump(example, f, default_flow_style=False, sort_keys=False)
    print(f"Config template written to {path}")
    print("Edit it, then run: python -m job_agent.main")
