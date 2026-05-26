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

_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tiff"})


def _is_image(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in _IMAGE_EXTS


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


def _build_page_blocks(
    entry: dict,
    media_url: str | None = None,
    filename: str | None = None,
) -> list[dict]:
    blocks = []

    # Media embed at the very top (image or file from LINE)
    if media_url and filename:
        if _is_image(filename):
            blocks.append({
                "object": "block",
                "type": "image",
                "image": {"type": "external", "external": {"url": media_url}},
            })
        else:
            blocks.append({
                "object": "block",
                "type": "file",
                "file": {"type": "external", "external": {"url": media_url}, "name": filename},
            })
    elif filename:
        # GitHub backup succeeded but URL unavailable (e.g. private repo)
        label = "圖片" if _is_image(filename) else "檔案"
        blocks.append(_callout(f"📎 原始{label}（{filename}）已備份至 GitHub", "📁"))

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


async def write_entry(
    entry: dict,
    media_url: str | None = None,
    filename: str | None = None,
) -> str:
    """Create a Notion Index page and write a Log entry. Returns the Index page URL."""
    now = datetime.now(timezone.utc).isoformat()
    category = entry.get("category", "Events")
    emoji = CATEGORY_EMOJI.get(category, "📝")
    tags = [{"name": t} for t in entry.get("tags", [])[:5]]

    props = {
        "Title": {"title": _rich_text(entry.get("title", "Untitled"))},
        "Category": {"select": {"name": category}},
        "Tags": {"multi_select": tags},
        "Date": {"date": {"start": now}},
        "Status": {"select": {"name": "Draft"}},
    }
    if entry.get("source_url"):
        props["Source URL"] = {"url": entry["source_url"]}

    page = await notion.pages.create(
        parent={"database_id": settings.notion_index_database_id},
        icon={"type": "emoji", "emoji": emoji},
        properties=props,
        children=_build_page_blocks(entry, media_url=media_url, filename=filename),
    )

    page_url = page.get("url", "")
    await _append_log(entry, page_url, now)
    return page_url


async def get_latest_theory() -> str:
    """Read Bruce's 20/20 theory from the Notion Log Database page titled '20/20理論設定'."""
    try:
        results = await notion.databases.query(
            database_id=settings.notion_log_database_id,
            filter={"property": "Title", "title": {"equals": "20/20理論設定"}},
            page_size=1,
        )
        pages = results.get("results", [])
        if not pages:
            return ""
        page_id = pages[0]["id"]
        blocks_response = await notion.blocks.children.list(block_id=page_id)
        parts = []
        for block in blocks_response.get("results", []):
            block_type = block.get("type", "")
            rich_text = block.get(block_type, {}).get("rich_text", [])
            for rt in rich_text:
                plain = rt.get("plain_text", "")
                if plain:
                    parts.append(plain)
        return "\n".join(parts)
    except Exception as e:
        print(f"[WARN] get_latest_theory failed: {e}")
        return ""


async def _append_log(entry: dict, page_url: str, timestamp: str):
    """Append one row to the Log database. Source URL always points to the Index page."""
    props = {
        "Title": {"title": _rich_text(entry.get("title", "Untitled"))},
        "Category": {"select": {"name": entry.get("category", "Events")}},
        "Timestamp": {"date": {"start": timestamp}},
    }
    if page_url:
        props["Source URL"] = {"url": page_url}
    await notion.pages.create(
        parent={"database_id": settings.notion_log_database_id},
        properties=props,
    )
