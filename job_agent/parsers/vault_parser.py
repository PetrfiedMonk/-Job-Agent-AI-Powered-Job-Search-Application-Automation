"""
Obsidian Vault Parser
Recursively reads all markdown files from your vault and extracts
structured content: work history, projects, skills, ideas, and raw notes.
"""
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import yaml


# Obsidian tags/folders that indicate relevant content
WORK_INDICATORS = ["work", "job", "career", "experience", "role", "position", "employer"]
PROJECT_INDICATORS = ["project", "build", "built", "created", "launched", "developed"]
SKILL_INDICATORS = ["skill", "technology", "tech", "tool", "framework", "learned", "expertise"]
IDEA_INDICATORS = ["idea", "concept", "vision", "goal", "plan", "strategy"]


def parse_frontmatter(content: str) -> Tuple[Dict, str]:
    """Extract YAML frontmatter and return (metadata, body)."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(content[3:end]) or {}
                return meta, content[end + 3:].strip()
            except yaml.YAMLError:
                pass
    return {}, content


def strip_obsidian_syntax(text: str) -> str:
    """Clean Obsidian-specific syntax for plain text extraction."""
    # [[wikilinks]] -> text
    text = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', r'\1', text)
    # ![[embeds]] -> remove
    text = re.sub(r'!\[\[[^\]]+\]\]', '', text)
    # #tags -> tag
    text = re.sub(r'(?<!\S)#(\w+)', r'\1', text)
    # ==highlights== -> text
    text = re.sub(r'==([^=]+)==', r'\1', text)
    # ~~strikethrough~~ -> remove
    text = re.sub(r'~~[^~]+~~', '', text)
    # Clean extra whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_tags(content: str, frontmatter: Dict) -> List[str]:
    """Extract all tags from frontmatter and inline #tags."""
    tags = []
    # Frontmatter tags
    if "tags" in frontmatter:
        fm_tags = frontmatter["tags"]
        if isinstance(fm_tags, list):
            tags.extend([str(t).lower() for t in fm_tags])
        elif isinstance(fm_tags, str):
            tags.extend([t.strip().lower() for t in fm_tags.split(",")])
    # Inline tags
    inline = re.findall(r'(?<!\S)#(\w+)', content)
    tags.extend([t.lower() for t in inline])
    return list(set(tags))


def categorize_note(filename: str, tags: List[str], content: str) -> str:
    """Categorize a note by its likely content type."""
    lower_name = filename.lower()
    lower_content = content[:500].lower()
    all_text = lower_name + " ".join(tags) + lower_content

    if any(w in all_text for w in WORK_INDICATORS):
        return "work"
    if any(w in all_text for w in PROJECT_INDICATORS):
        return "project"
    if any(w in all_text for w in SKILL_INDICATORS):
        return "skill"
    if any(w in all_text for w in IDEA_INDICATORS):
        return "idea"
    return "general"


class VaultParser:
    def __init__(self, vault_path: str):
        self.vault_path = Path(vault_path)
        if not self.vault_path.exists():
            raise ValueError(f"Vault path does not exist: {vault_path}")

    def parse(self) -> Dict:
        """
        Parse the entire vault and return structured content.

        Returns:
            {
                "notes": [{filename, path, category, tags, frontmatter, content, clean_text}],
                "by_category": {category: [notes]},
                "all_text": str,           # Full concatenated text for AI context
                "skills_mentioned": [str], # Unique skills found
                "companies_mentioned": [str],
                "summary_stats": {...}
            }
        """
        notes = []
        skipped = 0

        md_files = list(self.vault_path.rglob("*.md"))
        print(f"[vault] Found {len(md_files)} markdown files in vault")

        for filepath in md_files:
            # Skip hidden folders (.obsidian, .trash, etc.)
            parts = filepath.parts
            if any(p.startswith(".") for p in parts):
                skipped += 1
                continue

            try:
                raw = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"[vault] Warning: could not read {filepath}: {e}")
                skipped += 1
                continue

            if len(raw.strip()) < 20:  # Skip empty notes
                continue

            frontmatter, body = parse_frontmatter(raw)
            clean = strip_obsidian_syntax(body)
            tags = extract_tags(raw, frontmatter)
            category = categorize_note(filepath.stem, tags, clean)

            notes.append({
                "filename": filepath.stem,
                "path": str(filepath),
                "category": category,
                "tags": tags,
                "frontmatter": frontmatter,
                "content": body,
                "clean_text": clean,
            })

        by_category: Dict[str, List] = {}
        for note in notes:
            cat = note["category"]
            by_category.setdefault(cat, []).append(note)

        # Extract skills (common tech terms mentioned)
        all_text = "\n\n".join(
            f"=== {n['filename']} ===\n{n['clean_text']}" for n in notes
        )
        skills = self._extract_skills(all_text)
        companies = self._extract_companies(all_text)

        print(f"[vault] Parsed {len(notes)} notes | skipped {skipped}")
        for cat, items in by_category.items():
            print(f"  {cat}: {len(items)} notes")

        return {
            "notes": notes,
            "by_category": by_category,
            "all_text": all_text,
            "skills_mentioned": skills,
            "companies_mentioned": companies,
            "summary_stats": {
                "total_notes": len(notes),
                "skipped": skipped,
                "categories": {k: len(v) for k, v in by_category.items()},
            },
        }

    def _extract_skills(self, text: str) -> List[str]:
        """Pull out likely skill/technology mentions."""
        # Common tech terms to look for
        tech_patterns = [
            r'\bPython\b', r'\bJavaScript\b', r'\bTypeScript\b', r'\bReact\b',
            r'\bSQL\b', r'\bMySQL\b', r'\bPostgres\b', r'\bMongoDB\b',
            r'\bAWS\b', r'\bAzure\b', r'\bGCP\b', r'\bDocker\b', r'\bKubernetes\b',
            r'\bProduct Manager\b', r'\bProduct Management\b', r'\bScrum\b', r'\bAgile\b',
            r'\bJira\b', r'\bConfluence\b', r'\bFigma\b', r'\bTableau\b', r'\bPower BI\b',
            r'\bAPI\b', r'\bREST\b', r'\bGraphQL\b', r'\bCI/CD\b',
            r'\bAI\b', r'\bMachine Learning\b', r'\bML\b', r'\bLLM\b',
            r'\bNetwork\b', r'\bCCNA\b', r'\bCCNP\b', r'\bMCSE\b',
            r'\bArcGIS\b', r'\bGIS\b', r'\bSalesforce\b', r'\bHubSpot\b',
        ]
        found = set()
        for pattern in tech_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                found.add(re.sub(r'\\b', '', pattern).strip())
        return sorted(found)

    def _extract_companies(self, text: str) -> List[str]:
        """Find company names mentioned in the vault."""
        # Look for patterns like "at Company" or "@ Company"
        patterns = [
            r'(?:at|@|worked (?:at|for)|joined|left)\s+([A-Z][A-Za-z\s&,\.]+?)(?:\s+(?:as|in|from|until|\n))',
        ]
        companies = set()
        for pattern in patterns:
            for m in re.finditer(pattern, text):
                name = m.group(1).strip().rstrip(",.")
                if 3 < len(name) < 50:
                    companies.add(name)
        return sorted(companies)


def get_vault_summary_for_ai(vault_data: Dict, max_chars: int = 50000) -> str:
    """
    Prepare a condensed vault summary for AI context.
    Prioritizes work/project notes, truncates to fit context window.
    """
    sections = []

    # Work notes first (most valuable for resumes)
    for cat in ["work", "project", "skill", "idea", "general"]:
        notes = vault_data["by_category"].get(cat, [])
        if not notes:
            continue
        sections.append(f"\n## {cat.upper()} NOTES ({len(notes)} files)\n")
        for note in notes[:20]:  # Cap per category
            text = note["clean_text"][:1500]  # Cap per note
            sections.append(f"### {note['filename']}\n{text}\n")

    full = "\n".join(sections)
    if len(full) > max_chars:
        full = full[:max_chars] + "\n\n[... vault truncated for context ...]"
    return full
