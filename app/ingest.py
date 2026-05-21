import asyncio
import json
import re
import anthropic
from app.config import settings
from app.prompts import SYSTEM_PROMPT, INGEST_PROMPT, CLASSIFICATION_PROMPT, REGENERATE_QUESTIONS_PROMPT

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

_PARSE_ERROR_ENTRY = {
    "title": "Parse Error",
    "summary": "Failed to parse LLM response as JSON.",
    "structured_notes": "",
    "category": "Events",
    "category_confidence": "low",
    "suggested_new_category": None,
    "tags": [],
    "entities": {"people": [], "companies": [], "industries": [], "concepts": []},
    "deepening_questions": [],
    "language": "zh-TW",
    "_parse_error": True,
}

_FALLBACK_QUESTIONS = [
    "請問您對此內容有哪些延伸想法？",
    "這個事件對 BHRC 的業務有什麼啟示？",
]


def _extract_json(raw: str) -> dict:
    """Extract and parse JSON from LLM response, handling code fences and prose."""
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    candidate = match.group(1).strip() if match else raw.strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    return {**_PARSE_ERROR_ENTRY, "_raw_response": raw}


def classify_intent(message: str) -> str:
    """Returns 'ingest' or 'query'."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": CLASSIFICATION_PROMPT.format(message=message)}
        ],
    )
    result = _extract_json(response.content[0].text)
    return result.get("intent", "ingest")


def ingest_text(text: str, model: str, bruce_theory: str = "") -> dict:
    """Process text input and return structured knowledge entry."""
    theory_fill = bruce_theory if bruce_theory else "目前使用者尚未設定具體理論框架。請運用您內建的商業常識與專業視角進行深度摘要，並務必嚴格遵守輸出 JSON 格式的指令。"
    system = SYSTEM_PROMPT.replace("<<BRUCE_THEORY>>", theory_fill)
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0.2,
        system=system,
        messages=[
            {"role": "user", "content": INGEST_PROMPT.format(input_text=text)}
        ],
    )
    return _extract_json(response.content[0].text)


async def ingest_with_context(text: str, source_type: str, model: str, source_url: str = None) -> dict:
    """Ingest with additional source metadata and dynamic 20/20 theory."""
    from app.notion_writer import get_latest_theory

    prefix = ""
    if source_url:
        prefix += f"[Source URL: {source_url}]\n\n"
    if source_type != "text":
        prefix += f"[Input type: {source_type}]\n\n"

    bruce_theory = await get_latest_theory()
    entry = await asyncio.to_thread(ingest_text, prefix + text, model, bruce_theory)
    if source_url:
        entry["source_url"] = source_url
    return entry


def regenerate_questions(summary: str, previous_questions: list[str], model: str) -> list[str]:
    """Generate new deepening questions from a completely different angle."""
    prev_q_text = "\n".join(f"- {q}" for q in previous_questions)
    response = client.messages.create(
        model=model,
        max_tokens=400,
        temperature=0.2,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": REGENERATE_QUESTIONS_PROMPT.format(
                    summary=summary,
                    previous_questions=prev_q_text,
                ),
            }
        ],
    )
    result = _extract_json(response.content[0].text)
    # Use falsy check to catch both missing key and empty list from parse errors
    questions = result.get("deepening_questions")
    return questions if questions else _FALLBACK_QUESTIONS
