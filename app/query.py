import anthropic
from notion_client import AsyncClient
from app.config import settings
from app.prompts import SYSTEM_PROMPT, QUERY_PROMPT

notion = AsyncClient(auth=settings.notion_api_key)
claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)

_NO_RESULTS = "目前知識庫中找不到相關資料。建議您先記錄一些相關的想法或新聞，我就能幫您更準確地回答這個問題。"


async def _search_notion(query: str) -> list[dict]:
    """Search Notion database and return matching page summaries."""
    results = await notion.databases.query(
        database_id=settings.notion_index_database_id,
        filter={
            "or": [
                {"property": "Title", "title": {"contains": query[:50]}},
                {"property": "Tags", "multi_select": {"contains": query[:50]}},
            ]
        },
        page_size=5,
    )
    pages = []
    for page in results.get("results", []):
        props = page.get("properties", {})
        title_list = props.get("Title", {}).get("title", [])
        title = title_list[0].get("plain_text", "Untitled") if title_list else "Untitled"
        category = (props.get("Category", {}).get("select") or {}).get("name", "")
        pages.append({
            "title": title,
            "category": category,
            "url": page.get("url", ""),
            "id": page["id"],
        })
    return pages


async def _get_page_content(page_id: str) -> str:
    """Fetch block text content from a Notion page."""
    blocks = await notion.blocks.children.list(block_id=page_id)
    texts = []
    for block in blocks.get("results", []):
        block_type = block.get("type", "")
        rich_text = block.get(block_type, {}).get("rich_text", [])
        for rt in rich_text:
            texts.append(rt.get("plain_text", ""))
    return "\n".join(texts)[:3000]


async def answer_query(question: str) -> str:
    """Search Notion and synthesize an answer via Claude."""
    pages = await _search_notion(question)

    if not pages:
        return _NO_RESULTS

    context_parts = []
    for page in pages[:3]:
        content = await _get_page_content(page["id"])
        context_parts.append(f"### {page['title']} [{page['category']}]\n{content}")

    context = "\n\n---\n\n".join(context_parts)

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": QUERY_PROMPT.format(question=question, context=context),
            }
        ],
    )
    return response.content[0].text
