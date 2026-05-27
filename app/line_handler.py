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
from app.ingest import classify_intent, ingest_with_context
from app.query import answer_query
from app.notion_writer import write_entry
from app.github_client import backup_entry
from app.file_parser import parse_image_bytes, parse_document_bytes, fetch_url_content

configuration = Configuration(access_token=settings.line_channel_access_token)
handler = WebhookParser(settings.line_channel_secret)

GREETING_KEYWORDS = {"哈囉", "hello", "hi", "嗨", "測試", "test"}

MODEL_HAIKU = os.getenv("MODEL_HAIKU", "claude-haiku-4-5-20251001")
MODEL_SONNET = os.getenv("MODEL_SONNET", "claude-sonnet-4-6")

_ACK_MESSAGE = (
    "👌 已收到！正在為您淬取知識並同步至 BHRC 資料庫。"
    "因後台處理需時，請等待約 20~30 秒後再至 Notion 查看。"
    "如需調整或添加心得，請直接在 Notion 頁面編輯。"
)


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
        line_bot_blob_api = AsyncMessagingApiBlob(api_client)
        return await line_bot_blob_api.get_message_content(message_id)


async def _run_pipeline(
    user_id: str,
    entry: dict,
    raw_bytes: bytes,
    filename: str | None,
    embed_media: bool = False,
):
    """Write entry to Notion Index+Log and back up to GitHub.

    For image/file inputs (embed_media=True), backs up to GitHub first to obtain
    the raw URL, then embeds it as an External Image/File block in the Notion page.
    For URL/text inputs, writes to Notion and backs up to GitHub in parallel.
    """
    if entry.get("_parse_error"):
        raw_text = entry.get("_raw_response", "")
        await _push(user_id, f"⚠️ JSON 解析失敗，結尾內容：\n{raw_text[-200:]}")
        return

    if embed_media and filename:
        # Sequential: backup → get GitHub raw URL → write to Notion with embedded media
        github_url = None
        try:
            github_url = await backup_entry(entry, raw_bytes, filename)
        except Exception as e:
            print(f"[ERROR] GitHub backup failed: {e}")
            await _push(user_id, f"⚠️ GitHub 備份失敗：{e}")

        try:
            await write_entry(entry, media_url=github_url, filename=filename)
        except Exception as e:
            print(f"[ERROR] Notion write failed: {e}")
            await _push(user_id, f"⚠️ 儲存至 Notion 失敗：{e}")
    else:
        # Parallel: no media URL dependency between GitHub and Notion
        results = await asyncio.gather(
            write_entry(entry),
            backup_entry(entry, raw_bytes, filename),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        for e in errors:
            print(f"[ERROR] _run_pipeline: {e}")
        if errors:
            await _push(user_id, f"⚠️ 部分儲存失敗，請稍後至 Notion 確認：{errors[0]}")


async def _pipeline_url(user_id: str, url: str):
    try:
        content, final_url = await fetch_url_content(url)
        entry = await ingest_with_context(content, source_type="url", model=MODEL_HAIKU, source_url=final_url)
    except Exception as e:
        print(f"[ERROR] _pipeline_url: {e}")
        await _push(user_id, f"⚠️ 處理連結失敗：{e}")
        return
    await _run_pipeline(user_id, entry, raw_bytes=url.encode(), filename=None, embed_media=False)


async def _pipeline_youtube(user_id: str, url: str):
    await _push(user_id, "🎥 偵測到 YouTube 影片！影片解析專屬管線（Gemini 大腦）即將上線，敬請期待！")


async def _pipeline_text(user_id: str, text: str):
    try:
        entry = await ingest_with_context(text, source_type="text", model=MODEL_HAIKU)
    except Exception as e:
        print(f"[ERROR] _pipeline_text: {e}")
        await _push(user_id, f"⚠️ 文字分析失敗：{e}")
        return
    await _run_pipeline(user_id, entry, raw_bytes=text.encode(), filename=None, embed_media=False)


async def _pipeline_image(user_id: str, image_bytes: bytes, filename: str):
    try:
        description = await parse_image_bytes(image_bytes)
        entry = await ingest_with_context(description, source_type="image", model=MODEL_SONNET)
    except Exception as e:
        print(f"[ERROR] _pipeline_image: {e}")
        await _push(user_id, f"⚠️ 圖片分析失敗：{e}")
        return
    await _run_pipeline(user_id, entry, raw_bytes=image_bytes, filename=filename, embed_media=True)


async def _pipeline_file(user_id: str, file_bytes: bytes, filename: str):
    try:
        text_content = await asyncio.to_thread(parse_document_bytes, file_bytes, filename)
        entry = await ingest_with_context(text_content, source_type="file", model=MODEL_SONNET)
    except Exception as e:
        print(f"[ERROR] _pipeline_file: {e}")
        await _push(user_id, f"⚠️ 檔案解析失敗：{e}")
        return
    await _run_pipeline(user_id, entry, raw_bytes=file_bytes, filename=filename, embed_media=True)


async def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text.lower() in GREETING_KEYWORDS:
        await _push(user_id, "你好！請問今天有什麼科技趨勢或商業文章要讓我研讀的嗎？請直接丟給我連結或圖片！")
        return

    # URL — immediate ack, background pipeline
    if text.startswith("http://") or text.startswith("https://"):
        await _push(user_id, _ACK_MESSAGE)
        if "youtube.com" in text or "youtu.be" in text:
            asyncio.create_task(_pipeline_youtube(user_id, text))
        else:
            asyncio.create_task(_pipeline_url(user_id, text))
        return

    # Classify intent for plain text
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

    # Plain text ingest — immediate ack, background pipeline
    await _push(user_id, _ACK_MESSAGE)
    asyncio.create_task(_pipeline_text(user_id, text))


async def handle_image(event):
    user_id = event.source.user_id
    image_bytes = await _download_line_content(event.message.id)
    await _push(user_id, _ACK_MESSAGE)
    filename = f"{event.message.id}.jpg"
    asyncio.create_task(_pipeline_image(user_id, image_bytes, filename))


async def handle_file(event):
    user_id = event.source.user_id
    file_bytes = await _download_line_content(event.message.id)
    await _push(user_id, _ACK_MESSAGE)
    filename = event.message.file_name
    asyncio.create_task(_pipeline_file(user_id, file_bytes, filename))
