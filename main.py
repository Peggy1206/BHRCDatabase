from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
    FileMessageContent,
)
from app.line_handler import handler, handle_text, handle_image, handle_file

app = FastAPI(title="BHRC Knowledge Bot")


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _process_events(events: list):
    for event in events:
        if not isinstance(event, MessageEvent):
            continue
        try:
            if isinstance(event.message, TextMessageContent):
                await handle_text(event)
            elif isinstance(event.message, ImageMessageContent):
                await handle_image(event)
            elif isinstance(event.message, FileMessageContent):
                await handle_file(event)
        except Exception as e:
            print(f"[ERROR] Unhandled exception in webhook handler: {e}")


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = handler.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    background_tasks.add_task(_process_events, events)
    return {"status": "ok"}
