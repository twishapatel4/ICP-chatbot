"""Voice answers: turn a recording into text. The text then goes through the same answer path as
typed text, so every check on a typed answer applies to a spoken one."""

from pydantic import BaseModel

from icp_chatbot.config import Settings
from icp_chatbot.llm.types import LLM, StepConfig
from icp_chatbot.prompts import transcribe_prompt_for

# Formats the Gemini API documents for audio input.
ALLOWED_MIME_TYPES = frozenset(
    {"audio/wav", "audio/mp3", "audio/mpeg", "audio/aac", "audio/ogg", "audio/flac", "audio/aiff"}
)


class Transcript(BaseModel):
    text: str = ""


def normalize_mime(content_type: str | None) -> str | None:
    """'audio/wav; codecs=1' -> 'audio/wav'; None when the type is not one we accept."""
    base = (content_type or "").split(";")[0].strip().lower()
    base = "audio/wav" if base in ("audio/x-wav", "audio/wave") else base
    return base if base in ALLOWED_MIME_TYPES else None


async def transcribe(llm: LLM, settings: Settings, audio: bytes, mime_type: str, track: str = "sales"
) -> str:
    cfg = StepConfig(model=settings.model_transcribe, temperature=0.0, max_output_tokens=1024)
    result = await llm.generate(
        step="transcribe",
        cfg=cfg,
        system=transcribe_prompt_for(track),
        user="Transcribe the recording.",
        schema=Transcript,
        audio=(audio, mime_type),
    )
    return result.parsed.text.strip()[: settings.max_answer_chars]
