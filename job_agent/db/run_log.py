"""
RunLog — real-time markdown log written during an apply run.

Appends to output/run_log.md as each application completes so errors
and issues are immediately visible and documented for later review.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


_STATUS_ICON = {
    "applied":      "✅",
    "failed":       "❌",
    "needs_manual": "⚠️",
    "applying":     "🔄",
    "queued":       "⏳",
}


class RunLog:
    """
    Thread-safe markdown log. Call `start_run()` at the beginning of a batch,
    then `log_result()` after each application. Call `finish_run()` at the end.
    """

    def __init__(self, log_path: str = "./output/run_log.md"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._run_start: Optional[datetime] = None
        self._run_count = 0
        self._applied = 0
        self._failed = 0
        self._manual = 0

    def start_run(self, label: str = ""):
        """Write a run-header section to the log."""
        self._run_start = datetime.now()
        self._run_count = 0
        self._applied = 0
        self._failed = 0
        self._manual = 0
        ts = self._run_start.strftime("%Y-%m-%d %H:%M:%S")
        header = f"\n---\n\n## Run: {ts}"
        if label:
            header += f" — {label}"
        header += "\n\n"
        self._append(header)

    def log_result(
        self,
        *,
        title: str,
        company: str,
        url: str,
        status: str,
        ats: str = "",
        error: Optional[str] = None,
        notes: Optional[str] = None,
        fields_filled: int = 0,
    ):
        """Append one application result to the log. Thread-safe."""
        self._run_count += 1
        icon = _STATUS_ICON.get(status.lower(), "•")
        ts = datetime.now().strftime("%H:%M:%S")
        status_upper = status.upper()

        if status.lower() == "applied":
            self._applied += 1
        elif status.lower() == "failed":
            self._failed += 1
        elif status.lower() == "needs_manual":
            self._manual += 1

        lines = [
            f"### {icon} {status_upper} — {title} @ {company}",
            f"- **URL**: {url}",
        ]
        if ats:
            lines.append(f"- **ATS**: {ats}")
        if fields_filled:
            lines.append(f"- **Fields filled**: {fields_filled}")
        if error:
            # Truncate long errors, strip internal stack noise
            short_err = error[:300].replace("\n", " ").strip()
            lines.append(f"- **Error**: {short_err}")
        if notes:
            short_notes = notes[:200].replace("\n", " ").strip()
            lines.append(f"- **Notes**: {short_notes}")
        lines.append(f"- **Time**: {ts}")
        lines.append("")

        self._append("\n".join(lines) + "\n")

    def log_issue(self, message: str, context: str = ""):
        """Log a pipeline-level issue (CAPTCHA, network error, etc.)."""
        ts = datetime.now().strftime("%H:%M:%S")
        lines = [f"### ⚠️  ISSUE — {message}"]
        if context:
            lines.append(f"- **Context**: {context[:300]}")
        lines.append(f"- **Time**: {ts}")
        lines.append("")
        self._append("\n".join(lines) + "\n")

    def finish_run(self):
        """Write a summary footer for the run."""
        if not self._run_start:
            return
        elapsed = (datetime.now() - self._run_start).total_seconds()
        mins, secs = divmod(int(elapsed), 60)
        summary = (
            f"\n**Run summary**: {self._run_count} jobs — "
            f"✅ {self._applied} applied, "
            f"❌ {self._failed} failed, "
            f"⚠️ {self._manual} needs manual "
            f"({mins}m {secs}s)\n"
        )
        self._append(summary)

    def log_system_analysis(
        self,
        score: int,
        grade: str,
        delta: int,
        breakdown: dict,
        top_fixes: list,
    ):
        """
        Append a system performance analysis block after finish_run().
        This is the feedback loop: what drove the score, what to fix next.
        """
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        delta_label = "↑ improved" if delta > 0 else ("↓ regressed" if delta < 0 else "→ flat")

        lines = [
            "",
            "### 📊 System Analysis",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| **Score** | {score}/100 — Grade **{grade}** ({delta_str} pts {delta_label}) |",
            f"| Automation | {breakdown.get('automation_pts',0)}/40 pts |",
            f"| Reach | {breakdown.get('reach_pts',0)}/25 pts |",
            f"| Target Quality | {breakdown.get('quality_pts',0)}/20 pts |",
            f"| Velocity | {breakdown.get('velocity_pts',0)}/15 pts |",
            "",
        ]

        failure_costs = breakdown.get("failure_costs", {})
        if failure_costs:
            sorted_costs = sorted(failure_costs.items(), key=lambda x: x[1], reverse=True)
            lines += ["**Score lost by failure type:**", ""]
            for etype, cost in sorted_costs:
                lines.append(f"- `{etype}`: −{cost} pts")
            lines.append("")

        if top_fixes:
            lines += ["**Fix these to improve next run:**", ""]
            for item in top_fixes[:5]:
                cost = item.get("point_cost", 0)
                cat = item.get("category", "")
                ats = item.get("ats") or "any"
                seen = item.get("occurrences", 1)
                if cost > 0:
                    lines.append(f"- **+{cost} pts** — fix `{cat}` on {ats} ({seen}x seen)")
                else:
                    lines.append(f"- Fix `{cat}` on {ats} ({seen}x seen)")
            lines.append("")

        lines.append("")
        self._append("\n".join(lines))

    def _append(self, text: str):
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(text)

    @property
    def path(self) -> Path:
        return self.log_path
