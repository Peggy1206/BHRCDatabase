import asyncio
import base64
from datetime import datetime, timezone
from github import Github, GithubException
from app.config import settings

_gh = Github(settings.github_token)
_repo = _gh.get_repo(settings.github_repo)


def _safe_filename(title: str) -> str:
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)
    return safe.strip().replace(" ", "_")[:60]


def _build_markdown(entry: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tags = ", ".join(entry.get("tags", []))
    questions = "\n".join(f"- {q}" for q in entry.get("deepening_questions", []))
    entity_lines = [
        f"**{k.capitalize()}**: {', '.join(v)}"
        for k, v in entry.get("entities", {}).items()
        if v
    ]

    return f"""# {entry.get("title", "Untitled")}

**Category**: {entry.get("category", "")}
**Tags**: {tags}
**Ingested**: {now}

---

## Summary

{entry.get("summary", "")}

## Bruce's Insight

{entry.get("bruce_insight", "_No insight recorded yet._")}

## Structured Analysis

{entry.get("structured_notes", "")}

## Deepening Questions

{questions}

## Entities

{chr(10).join(entity_lines)}
"""


def _commit_file(path: str, content: bytes, message: str):
    """Create or update a file in the GitHub repo (synchronous)."""
    try:
        existing = _repo.get_contents(path, ref=settings.github_branch)
        _repo.update_file(path, message, content, existing.sha, branch=settings.github_branch)
    except GithubException:
        _repo.create_file(path, message, content, branch=settings.github_branch)


async def backup_entry(entry: dict, raw_content: bytes, filename: str | None = None):
    """Commit markdown wiki page and original file to GitHub without blocking event loop."""
    now = datetime.now(timezone.utc)
    date_prefix = now.strftime("%Y/%m")
    safe_title = _safe_filename(entry.get("title", "untitled"))
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    category_slug = entry.get("category", "events").lower().replace(" ", "_")

    commit_msg = (
        f"[{entry.get('category', 'Events')}] {entry.get('title', 'Untitled')}\n\n"
        f"Ingested: {now.isoformat()}"
    )

    # Markdown wiki page
    md_path = f"wiki/{category_slug}/{date_prefix}/{timestamp}_{safe_title}.md"
    await asyncio.to_thread(_commit_file, md_path, _build_markdown(entry).encode("utf-8"), commit_msg)

    # Original file
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        raw_path = f"raw/{category_slug}/{date_prefix}/{timestamp}_{safe_title}.{ext}"
    else:
        raw_path = f"raw/{category_slug}/{date_prefix}/{timestamp}_{safe_title}.txt"

    await asyncio.to_thread(_commit_file, raw_path, raw_content, commit_msg)
