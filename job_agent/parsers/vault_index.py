"""
Vault Index
Builds a persistent, lightweight index of the Obsidian vault: one entry per file
containing its hashtags and a short summary. On every job tailor call, we score
all files against the job's keywords/tags and read only the top-N most relevant
files — never the whole vault.

Why this is better than dumping raw text:
  - Profile synthesis: index overview (~2KB) + targeted work/project notes (~15KB)
    vs old approach: random 50KB dump
  - Per-job tailoring: ~4KB of files that actually match the job
    vs old approach: 8KB of arbitrarily-ordered text
  - The index rebuilds automatically when vault files change (checksum-based).
"""
import json
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from job_agent.parsers.vault_parser import (
    parse_frontmatter, extract_tags, strip_obsidian_syntax, categorize_note
)

INDEX_VERSION = 2
INDEX_FILENAME = ".job_agent_index.json"

# Stop-words to ignore when building keyword queries from job descriptions
_STOP = {
    'with', 'this', 'that', 'from', 'have', 'will', 'your', 'their', 'about',
    'into', 'then', 'when', 'what', 'also', 'more', 'been', 'they', 'team',
    'role', 'work', 'years', 'experience', 'ability', 'strong', 'skills',
    'looking', 'required', 'preferred', 'including', 'using', 'help', 'across',
    'provide', 'ensure', 'develop', 'support', 'manage', 'build', 'drive',
}


def _file_sig(path: Path) -> str:
    """Cheap file signature: mtime + size (no file read needed)."""
    st = path.stat()
    return f"{st.st_mtime:.0f}:{st.st_size}"


def _first_meaningful_paragraph(text: str, max_chars: int = 200) -> str:
    """
    Extract the first non-heading, non-empty paragraph from a note body.
    Falls back to the first non-empty line.
    """
    for para in text.split('\n\n'):
        para = para.strip()
        if not para:
            continue
        # Skip pure heading blocks
        if all(line.startswith('#') for line in para.splitlines() if line.strip()):
            continue
        # Strip inline markup for a clean summary
        clean = re.sub(r'[*_`~\[\]!]', '', para)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if len(clean) >= 20:
            return clean[:max_chars]
    # fallback: first non-empty line
    for line in text.splitlines():
        line = line.strip().lstrip('#').strip()
        if len(line) >= 10:
            return line[:max_chars]
    return ""


class VaultIndex:
    """
    Persistent index of an Obsidian vault.

    Usage:
        index = VaultIndex("/path/to/vault", index_dir="./output")
        index.build()                          # fast if cached and fresh

        # During profile synthesis
        overview = index.get_index_overview()  # compact: all tags + summaries (~2KB)
        work_notes = index.get_category_content(["work", "project"])  # full text

        # During per-job tailoring
        context = index.get_relevant_content(
            query_keywords=["product manager", "python", "agile"],
            query_tags=["product-management", "python"],
        )
    """

    def __init__(self, vault_path: str, index_dir: Optional[str] = None):
        self.vault_path = Path(vault_path)
        if not self.vault_path.exists():
            raise ValueError(f"Vault path not found: {vault_path}")

        # Store index alongside vault by default; output_dir is cleaner for CI
        if index_dir:
            Path(index_dir).mkdir(parents=True, exist_ok=True)
            self.index_path = Path(index_dir) / INDEX_FILENAME
        else:
            self.index_path = self.vault_path / INDEX_FILENAME

        self._data: Optional[Dict] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def build(self, force: bool = False) -> "VaultIndex":
        """
        Load index from cache if fresh, otherwise rebuild and save.
        Returns self so calls can be chained.
        """
        if not force:
            cached = self._load_cached()
            if cached and self._is_fresh(cached):
                tag_count = len(cached.get("tag_map", {}))
                file_count = len(cached.get("files", []))
                print(f"[vault_index] Cache hit: {file_count} files, {tag_count} unique tags")
                self._data = cached
                return self

        print(f"[vault_index] Building index from {self.vault_path}...")
        self._data = self._build_fresh()
        self._save(self._data)
        return self

    def get_index_overview(self) -> str:
        """
        Compact listing of ALL tags and file summaries — what Claude reads
        first during profile synthesis to understand the vault's full scope.
        Typically ~1-3KB regardless of vault size.
        """
        self._ensure_loaded()
        lines = [
            f"VAULT INDEX  ({len(self._data['files'])} notes | "
            f"{len(self._data['tag_map'])} unique tags)",
            "=" * 60,
        ]

        # Tag cloud sorted by frequency
        tag_freq = sorted(
            [(tag, len(ids)) for tag, ids in self._data["tag_map"].items()],
            key=lambda x: -x[1],
        )
        lines.append("\nTAG CLOUD (tag → file count):")
        row = []
        for tag, cnt in tag_freq[:40]:
            row.append(f"#{tag}({cnt})")
            if len(row) == 6:
                lines.append("  " + "  ".join(row))
                row = []
        if row:
            lines.append("  " + "  ".join(row))

        # Per-file one-liner: id | category | tags | summary
        lines.append("\nFILE SUMMARIES:")
        for f in self._data["files"]:
            tags_str = " ".join(f"#{t}" for t in f["tags"][:6])
            summary = f["summary"][:120]
            lines.append(f"  [{f['id']:03d}] [{f['category'][:4]}] {f['filename']}")
            lines.append(f"        {tags_str}")
            lines.append(f"        {summary}")

        return "\n".join(lines)

    def get_category_content(
        self,
        categories: List[str],
        max_files: int = 15,
        max_chars_per_file: int = 2000,
        max_total_chars: int = 20000,
    ) -> str:
        """
        Return full note content for files in the given categories.
        Used during profile synthesis to give Claude depth on work/project history.
        """
        self._ensure_loaded()
        selected = [
            f for f in self._data["files"]
            if f["category"] in categories
        ]
        # Sort by char_count desc — longer notes tend to have more detail
        selected.sort(key=lambda x: -x["char_count"])
        selected = selected[:max_files]

        return self._read_files(selected, max_chars_per_file, max_total_chars)

    def get_relevant_content(
        self,
        query_keywords: List[str],
        query_tags: Optional[List[str]] = None,
        max_files: int = 8,
        max_chars_per_file: int = 1500,
        max_total_chars: int = 5000,
    ) -> str:
        """
        Return content of vault files most relevant to a set of keywords/tags.
        Called once per job during resume tailoring.

        Scoring (per file):
          +10 per exact tag match
          + 5 per keyword hit in filename
          + 3 per keyword hit in summary
          + 2 bonus if category is work/project
        """
        self._ensure_loaded()
        scored = self._score_files(
            query_tags or [],
            query_keywords,
        )
        top = [f for _, f in scored[:max_files]]
        if not top:
            return ""

        header = (
            f"[Vault: {len(top)} relevant files retrieved "
            f"(out of {len(self._data['files'])} indexed)]\n\n"
        )
        return header + self._read_files(top, max_chars_per_file, max_total_chars)

    def all_tags(self) -> List[str]:
        """Return every unique tag in the vault."""
        self._ensure_loaded()
        return list(self._data.get("tag_map", {}).keys())

    # ── Internals ─────────────────────────────────────────────────────────────

    def _ensure_loaded(self):
        if not self._data:
            self.build()

    def _score_files(
        self, query_tags: List[str], query_keywords: List[str]
    ) -> List[Tuple[int, Dict]]:
        tag_set = {t.lstrip('#').lower() for t in query_tags}
        kw_set = {k.lower() for k in query_keywords if len(k) > 3 and k.lower() not in _STOP}

        scored = []
        for f in self._data["files"]:
            score = 0
            file_tags = set(f["tags"])
            summary_lower = (f["summary"] or "").lower()
            filename_lower = f["filename"].lower()

            score += len(file_tags & tag_set) * 10
            for kw in kw_set:
                if kw in filename_lower:
                    score += 5
                if kw in summary_lower:
                    score += 3
            if f["category"] in ("work", "project"):
                score += 2

            if score > 0:
                scored.append((score, f))

        scored.sort(key=lambda x: -x[0])
        return scored

    def _read_files(
        self,
        files: List[Dict],
        max_chars_per_file: int,
        max_total_chars: int,
    ) -> str:
        chunks = []
        total = 0

        for f in files:
            filepath = self.vault_path / f["rel_path"]
            try:
                raw = filepath.read_text(encoding="utf-8", errors="replace")
                _, body = parse_frontmatter(raw)
                content = strip_obsidian_syntax(body)[:max_chars_per_file]
            except Exception:
                content = f["summary"]

            tags_str = "  ".join(f"#{t}" for t in f["tags"][:8])
            chunk = f"### {f['filename']}\nTags: {tags_str}\n\n{content}"

            if total + len(chunk) > max_total_chars:
                remaining = max_total_chars - total
                if remaining > 300:
                    chunks.append(chunk[:remaining] + "\n[truncated]")
                break

            chunks.append(chunk)
            total += len(chunk)

        return "\n\n---\n\n".join(chunks)

    def _build_fresh(self) -> Dict:
        md_files = [
            f for f in self.vault_path.rglob("*.md")
            if not any(p.startswith(".") for p in f.parts)
        ]

        files: List[Dict] = []
        tag_map: Dict[str, List[int]] = {}

        for filepath in md_files:
            try:
                raw = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if len(raw.strip()) < 20:
                continue

            frontmatter, body = parse_frontmatter(raw)
            clean = strip_obsidian_syntax(body)
            tags = extract_tags(raw, frontmatter)

            # Prefer explicit front-matter description/summary over extracted paragraph
            summary = (
                frontmatter.get("description")
                or frontmatter.get("summary")
                or frontmatter.get("subtitle")
                or _first_meaningful_paragraph(clean)
            )

            category = categorize_note(filepath.stem, tags, clean)
            file_id = len(files)
            rel_path = str(filepath.relative_to(self.vault_path))

            entry = {
                "id": file_id,
                "filename": filepath.stem,
                "rel_path": rel_path,
                "tags": tags,
                "summary": (summary or "")[:200],
                "category": category,
                "char_count": len(raw),
                "sig": _file_sig(filepath),
            }
            files.append(entry)

            for tag in tags:
                tag_map.setdefault(tag, []).append(file_id)

        data = {
            "version": INDEX_VERSION,
            "vault_path": str(self.vault_path),
            "built_at": datetime.now().isoformat(),
            "files": files,
            "tag_map": tag_map,
        }
        print(
            f"[vault_index] Built: {len(files)} files  |  "
            f"{len(tag_map)} unique tags  |  "
            f"saved → {self.index_path}"
        )
        return data

    def _is_fresh(self, cached: Dict) -> bool:
        """
        True if the cached index still reflects the current vault state.
        Checks: version match, vault path, file count, spot-check of 15 file sigs.
        """
        if cached.get("version") != INDEX_VERSION:
            return False
        if cached.get("vault_path") != str(self.vault_path):
            return False

        current_files = [
            f for f in self.vault_path.rglob("*.md")
            if not any(p.startswith(".") for p in f.parts)
        ]
        if len(current_files) != len(cached.get("files", [])):
            return False

        # Spot-check up to 15 files for signature changes
        cached_sigs = {f["rel_path"]: f.get("sig", "") for f in cached["files"]}
        for filepath in current_files[:15]:
            rel = str(filepath.relative_to(self.vault_path))
            if rel not in cached_sigs:
                return False
            if cached_sigs[rel] != _file_sig(filepath):
                return False

        return True

    def _load_cached(self) -> Optional[Dict]:
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def _save(self, data: Dict):
        try:
            with open(self.index_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[vault_index] Warning: could not save index: {e}")


# ── Convenience helpers ───────────────────────────────────────────────────────

def keywords_from_job_description(title: str, description: str, max_keywords: int = 60) -> List[str]:
    """
    Extract meaningful keywords from a job title + description for index queries.
    Strips stop-words, deduplicates, preserves important multi-word phrases.
    """
    text = f"{title} {description[:1500]}"
    # Extract individual words (4+ chars)
    words = [w.lower() for w in re.findall(r'\b[a-zA-Z]{4,}\b', text)]
    # Extract hyphenated / slash compounds (e.g. "cross-functional", "B2B/B2C")
    compounds = re.findall(r'\b[A-Za-z][\w/-]{3,}\b', text)
    all_terms = words + [c.lower() for c in compounds]
    seen, result = set(), []
    for term in all_terms:
        if term not in _STOP and term not in seen:
            seen.add(term)
            result.append(term)
    return result[:max_keywords]
