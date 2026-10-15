"""Build the SDK client and the adapter from settings."""

import inspect

from google import genai
from google.genai import types

from icp_chatbot.config import Settings
from icp_chatbot.llm.adapter import GeminiLLM
from icp_chatbot.llm.types import LLMError


def make_client(settings: Settings):
    http_options = types.HttpOptions(
        timeout=int(settings.llm_timeout_seconds * 1000),  # the SDK takes milliseconds
        retry_options=types.HttpRetryOptions(
            attempts=3,
            initial_delay=1.0,
            max_delay=8.0,
            http_status_codes=[429, 500, 502, 503, 504],
        ),
    )
    if settings.llm_backend == "developer":
        if not settings.gemini_api_key:
            raise LLMError("config", "GEMINI_API_KEY is required for the developer backend")
        return genai.Client(api_key=settings.gemini_api_key, http_options=http_options)

    if not settings.google_cloud_project:
        raise LLMError("config", "GOOGLE_CLOUD_PROJECT is required for the vertex backend")
    # Current docs use `enterprise=True`; older SDK versions use `vertexai=True`.
    params = inspect.signature(genai.Client.__init__).parameters
    flag = "enterprise" if "enterprise" in params else "vertexai"
    return genai.Client(
        **{flag: True},
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
        http_options=http_options,
    )


def make_llm(settings: Settings) -> GeminiLLM:
    return GeminiLLM(
        make_client(settings),
        timeout_s=settings.llm_timeout_seconds,
        send_labels=settings.llm_backend == "vertex",
    )
