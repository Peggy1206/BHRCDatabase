import asyncio
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhook import WebhookParser

from app.config import settings
from app.ingest import classify_intent, ingest_with_context, regenerate_questions
from app.query import answer_query
from app.notion_writer import write_entry
from app.github_client import backup_entry
from app.file_parser import parse_image_bytes, parse_document_bytes, fetch_url_content

configuration = Configuration(access_token=settings.line_channel_access_token)
handler = WebhookParser(settings.line_channel_secret)

# State machine keyed by LINE user_id
# Shape: { user_id: { "state": str, "entry": dict, "raw": bytes, "filename": str|None } }
USER_STATES: dict[str, dict] = {}

STATE_IDLE = "IDLE"
STATE_WAITING = "WAITING_FOR_INSIGHT"

REGEN_KEYWORDS = {"換問題", "不滿意", "重新提問", "換個角度", "換一個", "再問一次"}


async def _reply(reply_token: str, text: str):
    async with AsyncApiClient(configuration) as api_client:
        line_bot_api = AsyncMessagingApi(api_client)
        await line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])],
            )
        )


async def _download_line_content(message_id: str) -> bytes:
    async with AsyncApiClient(configuration) as api_client:
        line_bot_api = AsyncMessagingApi(api_client)
        return await line_bot_api.get_message_content(message_id)


def _is_regen_request(text: str) -> bool:
    return any(kw in text for kw in REGEN_KEYWORDS)


def _format_questions(questions: list[str]) -> str:
    return "\n".join(f"• {q}" for q in questions[:2])


def _build_preview_reply(entry: dict) -> str:
    questions = entry.get("deepening_questions", [])
    new_cat = f"\n💡 建議新增分類：{entry['suggested_new_category']}" if entry.get("suggested_new_category") else ""

    reply = (
        f"📰 已讀取內容！\n\n"
        f"{entry['title']}\n"
        f"{entry['summary']}\n\n"
        f"📂 分類：{entry['category']}{new_cat}\n"
        f"🏷️ 標籤：{', '.join(entry.get('tags', [])[:5])}"
    )
    if questions:
        reply += f"\n\n🤔 延伸思考：\n{_format_questions(questions)}"
    reply += "\n\n💬 請分享您的看法，或輸入「換問題」讓我換個角度再問您。"
    return reply


async def _handle_ingest_input(reply_token: str, user_id: str, entry: dict, raw: bytes, filename: str | None):
    """Store entry in state machine and prompt Bruce for insight."""
    if entry.get("_parse_error"):
        await _reply(reply_token, "抱歉，這次處理時遇到問題，請再試一次。")
        return
    USER_STATES[user_id] = {"state": STATE_WAITING, "entry": entry, "raw": raw, "filename": filename}
    await _reply(reply_token, _build_preview_reply(entry))


async def _handle_regen(reply_token: str, user_id: str):
    """Regenerate questions from a different angle, stay in WAITING state."""
    state = USER_STATES[user_id]
    entry = state["entry"]
    new_questions = regenerate_questions(
        summary=entry.get("summary", ""),
        previous_questions=entry.get("deepening_questions", []),
    )
    entry["deepening_questions"] = new_questions
    reply = f"好的，換個角度考考你！🤔\n\n{_format_questions(new_questions)}\n\n💬 請分享您的想法，或再次輸入「換問題」。"
    await _reply(reply_token, reply)


async def _commit_entry(reply_token: str, user_id: str, insight: str):
    """Attach Bruce's insight and persist to Notion + GitHub."""
    state = USER_STATES.pop(user_id, None)
    if not state:
        await _reply(reply_token, "找不到暫存資料，請重新輸入。")
        return

    entry = state["entry"]
    entry["bruce_insight"] = insight

    await asyncio.gather(
        asyncio.create_task(write_entry(entry)),
        asyncio.create_task(backup_entry(entry, state.get("raw", insight.encode()), state.get("filename"))),
    )
    await _reply(reply_token, "✅ 心得已整合，BHRC 資料庫已同步更新！")


async def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    current_state = USER_STATES.get(user_id, {}).get("state", STATE_IDLE)

    if current_state == STATE_WAITING:
        if _is_regen_request(text):
            await _handle_regen(event.reply_token, user_id)
        else:
            await _commit_entry(event.reply_token, user_id, insight=text)
        return

    # IDLE: classify intent
    intent = classify_intent(text)
    if intent == "query":
        answer = await answer_query(text)
        await _reply(event.reply_token, answer)
        return

    # Ingest
    if text.startswith("http://") or text.startswith("https://"):
        content, url = await fetch_url_content(text)
        entry = ingest_with_context(content, source_type="url", source_url=url)
    else:
        entry = ingest_with_context(text, source_type="text")

    await _handle_ingest_input(event.reply_token, user_id, entry, raw=text.encode(), filename=None)


async def handle_image(event):
    user_id = event.source.user_id
    if USER_STATES.get(user_id, {}).get("state") == STATE_WAITING:
        await _reply(event.reply_token, "請先分享您對上一則內容的看法，再傳送新的圖片。")
        return

    image_bytes = await _download_line_content(event.message.id)
    description = await parse_image_bytes(image_bytes)
    entry = ingest_with_context(description, source_type="image")
    filename = f"{event.message.id}.jpg"
    await _handle_ingest_input(event.reply_token, user_id, entry, raw=image_bytes, filename=filename)


async def handle_file(event):
    user_id = event.source.user_id
    if USER_STATES.get(user_id, {}).get("state") == STATE_WAITING:
        await _reply(event.reply_token, "請先分享您對上一則內容的看法，再傳送新的檔案。")
        return

    file_bytes = await _download_line_content(event.message.id)
    filename = event.message.file_name
    text_content = parse_document_bytes(file_bytes, filename)
    entry = ingest_with_context(text_content, source_type="file")
    await _handle_ingest_input(event.reply_token, user_id, entry, raw=file_bytes, filename=filename)
