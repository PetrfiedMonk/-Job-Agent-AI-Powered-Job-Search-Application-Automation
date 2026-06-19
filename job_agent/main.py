"""
Job Agent CLI
Usage:
    python -m job_agent.main setup          # Create config.yaml template
    python -m job_agent.main run            # Full pipeline (search + apply)
    python -m job_agent.main search         # Search & score only
    python -m job_agent.main apply          # Apply to queued jobs
    python -m job_agent.main dashboard      # Show pipeline stats
    python -m job_agent.main test-profile   # Build & display your profile
"""
import sys
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="job-agent",
        description="AI-powered job search and application agent",
    )
    parser.add_argument(
        "command",
        choices=["setup", "run", "search", "apply", "dashboard", "test-profile"],
        help="Command to run",
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--min-score", "-s",
        type=float,
        default=65.0,
        help="Minimum job fit score to consider (0-100, default: 65)",
    )
    parser.add_argument(
        "--max-apply", "-n",
        type=int,
        default=None,
        help="Max number of applications per run",
    )
    parser.add_argument(
        "--live", "-l",
        action="store_true",
        help="Enable auto_submit (actually submit applications)",
    )

    args = parser.parse_args()

    if args.command == "setup":
        from job_agent.config import save_example_config
        save_example_config("config.yaml")
        print("\nNext steps:")
        print("  1. Edit config.yaml with your info and paths")
        print("  2. Set your ANTHROPIC_API_KEY environment variable")
        print("  3. Run: python -m job_agent.main test-profile")
        print("  4. Run: python -m job_agent.main search")
        print("  5. Run: python -m job_agent.main run  (when ready)")
        return

    # Load config
    if not Path(args.config).exists() and args.command != "setup":
        print(f"Config not found: {args.config}")
        print("Run: python -m job_agent.main setup")
        sys.exit(1)

    from job_agent.config import load_config
    config = load_config(args.config)

    if not config.ai.anthropic_api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        print("Set it in config.yaml or as environment variable: ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    # Apply --live flag
    if args.live:
        config.automation.auto_submit = True
        print("⚠️  LIVE MODE: Applications will be auto-submitted!")

    from job_agent.orchestrator import JobOrchestrator
    agent = JobOrchestrator(config)

    if args.command == "run":
        agent.run(min_score=args.min_score, max_apply=args.max_apply)

    elif args.command == "search":
        agent.search_and_score(min_score=args.min_score)

    elif args.command == "apply":
        agent.apply_queued(max_apply=args.max_apply or 10)

    elif args.command == "dashboard":
        agent.dashboard()

    elif args.command == "test-profile":
        profile = agent.load_profile()
        print("\n" + "="*60)
        print("  YOUR AI-SYNTHESIZED PROFILE")
        print("="*60)
        print(f"Name:     {profile.name}")
        print(f"Email:    {profile.email}")
        print(f"Phone:    {profile.phone}")
        print(f"Location: {profile.location}")
        print(f"\nSUMMARY:\n{profile.summary}")
        print(f"\nSKILLS ({len(profile.skills)}):\n{', '.join(profile.skills[:20])}")
        print(f"\nEXPERIENCE ({len(profile.experience)} roles):")
        for exp in profile.experience[:3]:
            print(f"  • {exp.title} @ {exp.company}")
        print(f"\nUNIQUE VALUE PROPS:")
        for uvp in profile.unique_value_props:
            print(f"  ✓ {uvp}")
        print("="*60)


if __name__ == "__main__":
    main()
