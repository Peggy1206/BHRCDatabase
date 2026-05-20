from datetime import datetime, timezone
from notion_client import AsyncClient
from app.config import settings

notion = AsyncClient(auth=settings.notion_api_key)

CATEGORY_EMOJI = {
    "Events": "📰",
    "History": "📜",
    "Stories": "💬",
    "Company Performance": "📊",
    "Frameworks": "🧠",
}


def _rich_text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content[:2000]}}]


def _heading(text: str, level: int = 2) -> dict:
    return {
        "object": "block",
        "type": f"heading_{level}",
        f"heading_{level}": {"rich_text": _rich_text(text)},
    }


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }


def _callout(text: str, emoji: str = "💡") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rich_text(text),
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def _build_page_blocks(entry: dict) -> list[dict]:
    blocks = []

    if entry.get("bruce_insight"):
        blocks.append(_heading("💡 Bruce 的心得", 2))
        blocks.append(_callout(entry["bruce_insight"], "🧠"))

    blocks.append(_heading("摘要", 2))
    blocks.append(_paragraph(entry.get("summary", "")))

    if entry.get("structured_notes"):
        blocks.append(_heading("結構化分析", 2))
        blocks.append(_paragraph(entry["structured_notes"]))

    questions = entry.get("deepening_questions", [])
    if questions:
        blocks.append(_heading("延伸思考問題", 2))
        for q in questions:
            blocks.append(_paragraph(f"• {q}"))

    entities = entry.get("entities", {})
    entity_parts = []
    for key, values in entities.items():
        if values:
            entity_parts.append(f"**{key.capitalize()}**: {', '.join(values)}")
    if entity_parts:
        blocks.append(_heading("相關實體", 2))
        blocks.append(_paragraph("\n".join(entity_parts)))

    return blocks


async def write_entry(entry: dict) -> str:
    """Create a Notion page for the entry and log it. Returns the page URL."""
    now = datetime.now(timezone.utc).isoformat()
    category = entry.get("category", "Events")
    emoji = CATEGORY_EMOJI.get(category, "📝")
    tags = [{"name": t} for t in entry.get("tags", [])[:5]]

    props = {
        "Title": {"title": _rich_text(entry.get("title", "Untitled"))},
        "Category": {"select": {"name": category}},
        "Tags": {"multi_select": tags},
        "Date": {"date": {"start": now}},
        "Status": {"select": {"name": "Draft" if not entry.get("bruce_insight") else "Complete"}},
    }
    if entry.get("source_url"):
        props["Source URL"] = {"url": entry["source_url"]}
    page = await notion.pages.create(
        parent={"database_id": settings.notion_index_database_id},
        icon={"type": "emoji", "emoji": emoji},
        properties=props,
        children=_build_page_blocks(entry),
    )

    page_url = page.get("url", "")
    await _append_log(entry, page_url, now)
    return page_url


async def _append_log(entry: dict, page_url: str, timestamp: str):
    """Append one row to the log database."""
    props = {
        "Title": {"title": _rich_text(entry.get("title", "Untitled"))},
        "Category": {"select": {"name": entry.get("category", "Events")}},
        "Timestamp": {"date": {"start": timestamp}},
        "Page": {"url": page_url},
    }
    if entry.get("source_url"):
        props["Source URL"] = {"url": entry["source_url"]}
    await notion.pages.create(
        parent={"database_id": settings.notion_log_database_id},
        properties=props,
    )
