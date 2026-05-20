import asyncio
import os
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
    Configuration,
    PushMessageRequest,
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
# Shape: { user_id: { "state": str, "entry": dict, "insight": str, "raw": bytes, "filename": str|None } }
USER_STATES: dict[str, dict] = {}

STATE_IDLE = "IDLE"
STATE_WAITING_INSIGHT = "WAITING_FOR_INSIGHT"
STATE_CONFIRM_SAVE = "CONFIRM_SAVE"

CANCEL_KEYWORDS = {"不用存", "取消", "skip", "結束", "下一個"}
REGEN_KEYWORDS = {"換問題", "不滿意", "重新提問", "換個角度", "換一個", "再問一次"}
GREETING_KEYWORDS = {"哈囉", "hello", "hi", "嗨", "測試", "test"}

MODEL_HAIKU = os.getenv("MODEL_HAIKU", "claude-haiku-4-5-20251001")
MODEL_SONNET = os.getenv("MODEL_SONNET", "claude-sonnet-4-6")
REGEN_SONNET_THRESHOLD = 2


async def _push(user_id: str, text: str):
    async with AsyncApiClient(configuration) as api_client:
        line_bot_api = AsyncMessagingApi(api_client)
        await line_bot_api.push_message(
            PushMessageRequest(
                to=user_id,
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


async def _handle_ingest_input(user_id: str, entry: dict, raw: bytes, filename: str | None):
    """Store entry in state and prompt Bruce for insight. Does NOT write to Notion."""
    if entry.get("_parse_error"):
        await _push(user_id, "抱歉，這次處理時遇到問題，請再試一次。")
        return
    USER_STATES[user_id] = {
        "state": STATE_WAITING_INSIGHT,
        "entry": entry,
        "raw": raw,
        "filename": filename,
        "regen_count": 0,
    }
    await _push(user_id, _build_preview_reply(entry))


async def _handle_regen(user_id: str):
    """Regenerate questions from a different angle, stay in WAITING_INSIGHT state."""
    state = USER_STATES[user_id]
    entry = state["entry"]
    state["regen_count"] = state.get("regen_count", 0) + 1
    regen_count = state["regen_count"]

    model = MODEL_HAIKU if regen_count <= REGEN_SONNET_THRESHOLD else MODEL_SONNET
    prefix = "" if regen_count <= REGEN_SONNET_THRESHOLD else "🤖 來回激盪已達極限，已為您召喚 Sonnet 進階大腦深度解析：\n\n"

    try:
        new_questions = await asyncio.to_thread(
            regenerate_questions,
            summary=entry.get("summary", ""),
            previous_questions=entry.get("deepening_questions", []),
            model=model,
        )
    except Exception as e:
        print(f"[ERROR] regenerate_questions failed: {e}")
        return
    entry["deepening_questions"] = new_questions
    reply = f"{prefix}好的，換個角度考考你！🤔\n\n{_format_questions(new_questions)}\n\n💬 請分享您的想法，或再次輸入「換問題」。"
    await _push(user_id, reply)


async def _save_insight_pending(user_id: str, insight: str):
    """Save Bruce's insight to state and request final confirmation. Does NOT write to Notion."""
    state = USER_STATES.get(user_id)
    if not state:
        await _push(user_id, "找不到暫存資料，請重新輸入。")
        return
    state["insight"] = insight
    state["state"] = STATE_CONFIRM_SAVE
    await _push(
        user_id,
        "💡 Bruce 的心得已收到！\n\n確定要將本篇摘要與心得同步至 BHRC 知識庫嗎？\n請回覆：【確認儲存】 或 【取消】",
    )


async def _commit_entry(user_id: str):
    """Read insight from state, then persist entry to Notion + GitHub."""
    state = USER_STATES.pop(user_id, None)
    if not state:
        await _push(user_id, "找不到暫存資料，請重新輸入。")
        return

    entry = state["entry"]
    entry["bruce_insight"] = state.get("insight", "")

    await asyncio.gather(
        asyncio.create_task(write_entry(entry)),
        asyncio.create_task(backup_entry(entry, state.get("raw", b""), state.get("filename"))),
    )
    await _push(user_id, "✅ 心得已整合，BHRC 資料庫已同步更新！")


async def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    current_state = USER_STATES.get(user_id, {}).get("state", STATE_IDLE)

    # ── 全域萬能終止指令（最高優先，任何狀態皆有效）──
    if any(kw in text for kw in CANCEL_KEYWORDS):
        USER_STATES.pop(user_id, None)
        await _push(user_id, "👌 已為您取消本次對話，狀態已完全重置，未寫入任何資料庫。")
        return

    # ── 日常問候攔截（精準比對，任何狀態皆有效）──
    if text.lower() in GREETING_KEYWORDS:
        await _push(user_id, "你好！請問今天有什麼科技趨勢或商業文章要讓我研讀的嗎？請直接丟給我連結或圖片！")
        return

    # ── 第二階段：等待心得輸入 ──
    if current_state == STATE_WAITING_INSIGHT:
        if _is_regen_request(text):
            await _handle_regen(user_id)
        else:
            await _save_insight_pending(user_id, text)
        return

    # ── 第三階段：等待最終確認 ──
    if current_state == STATE_CONFIRM_SAVE:
        if "確認儲存" in text:
            await _commit_entry(user_id)
        else:
            USER_STATES.pop(user_id, None)
            await _push(user_id, "👌 已為您取消本次對話，狀態已完全重置，未寫入任何資料庫。")
        return

    # ── IDLE：意圖分類 ──
    try:
        intent = await asyncio.to_thread(classify_intent, text)
    except Exception as e:
        print(f"[ERROR] classify_intent failed: {e}")
        return

    if intent == "query":
        try:
            answer = await answer_query(text, model=MODEL_HAIKU)
        except Exception as e:
            print(f"[ERROR] answer_query failed: {e}")
            return
        await _push(user_id, answer)
        return

    # ── 第一階段：擷取內容，產生摘要，絕不寫入 Notion ──
    if text.startswith("http://") or text.startswith("https://"):
        try:
            content, url = await fetch_url_content(text)
        except Exception as e:
            print(f"[ERROR] fetch_url_content failed: {e}")
            return
        try:
            entry = await ingest_with_context(content, source_type="url", model=MODEL_HAIKU, source_url=url)
        except Exception as e:
            print(f"[ERROR] ingest_with_context failed: {e}")
            return
    else:
        try:
            entry = await ingest_with_context(text, source_type="text", model=MODEL_HAIKU)
        except Exception as e:
            print(f"[ERROR] ingest_with_context failed: {e}")
            return

    await _handle_ingest_input(user_id, entry, raw=text.encode(), filename=None)


async def handle_image(event):
    user_id = event.source.user_id
    current_state = USER_STATES.get(user_id, {}).get("state", STATE_IDLE)
    if current_state in (STATE_WAITING_INSIGHT, STATE_CONFIRM_SAVE):
        await _push(user_id, "請先完成目前的存檔流程，再傳送新的圖片。")
        return

    image_bytes = await _download_line_content(event.message.id)
    try:
        description = await parse_image_bytes(image_bytes)
    except Exception as e:
        print(f"[ERROR] parse_image_bytes failed: {e}")
        return
    try:
        entry = await ingest_with_context(description, source_type="image", model=MODEL_SONNET)
    except Exception as e:
        print(f"[ERROR] ingest_with_context failed: {e}")
        return
    filename = f"{event.message.id}.jpg"
    await _handle_ingest_input(user_id, entry, raw=image_bytes, filename=filename)


async def handle_file(event):
    user_id = event.source.user_id
    current_state = USER_STATES.get(user_id, {}).get("state", STATE_IDLE)
    if current_state in (STATE_WAITING_INSIGHT, STATE_CONFIRM_SAVE):
        await _push(user_id, "請先完成目前的存檔流程，再傳送新的檔案。")
        return

    file_bytes = await _download_line_content(event.message.id)
    filename = event.message.file_name
    try:
        text_content = await asyncio.to_thread(parse_document_bytes, file_bytes, filename)
    except Exception as e:
        print(f"[ERROR] parse_document_bytes failed: {e}")
        return
    try:
        entry = await ingest_with_context(text_content, source_type="file", model=MODEL_SONNET)
    except Exception as e:
        print(f"[ERROR] ingest_with_context failed: {e}")
        return
    await _handle_ingest_input(user_id, entry, raw=file_bytes, filename=filename)
