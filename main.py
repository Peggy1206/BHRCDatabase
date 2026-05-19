from fastapi import FastAPI, Request, HTTPException
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


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = await handler.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if not isinstance(event, MessageEvent):
            continue
        if isinstance(event.message, TextMessageContent):
            await handle_text(event)
        elif isinstance(event.message, ImageMessageContent):
            await handle_image(event)
        elif isinstance(event.message, FileMessageContent):
            await handle_file(event)

    return {"status": "ok"}
