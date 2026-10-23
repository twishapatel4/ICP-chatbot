import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from icp_chatbot.config import Settings
from icp_chatbot.llm import factory
from icp_chatbot.llm.adapter import GeminiLLM
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError, StepConfig, Usage


class Answer(BaseModel):
    text: str


def meta(prompt=100, out=20, thoughts=0, cached=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=out,
        thoughts_token_count=thoughts,
        cached_content_token_count=cached,
    )


def reply(parsed=None, text=None, usage=None):
    return SimpleNamespace(parsed=parsed, text=text, usage_metadata=usage or meta())


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome


def client_with(*outcomes):
    models = FakeModels(outcomes)
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


CFG = StepConfig(model="gemini-2.5-pro", thinking_budget=512)


async def call(llm, cfg=CFG, labels=None):
    return await llm.generate(
        step="extraction", cfg=cfg, system="sys", user="hello", schema=Answer, labels=labels
    )


async def test_returns_parsed_output_usage_and_cost():
    client, models = client_with(reply(parsed=Answer(text="hi"), usage=meta(1000, 200, 50)))
    result = await call(GeminiLLM(client))
    assert result.parsed == Answer(text="hi")
    assert result.usage == Usage(1000, 200, 50, 0)
    # 1000 in at 1.25/M + 250 output-side tokens at 10/M
    assert result.cost_usd == pytest.approx(0.00125 + 0.0025)
    request = models.calls[0]
    assert request.model == "gemini-2.5-pro" and request.contents == "hello"
    assert request.config.system_instruction == "sys"
    assert request.config.response_mime_type == "application/json"
    assert request.config.response_schema is Answer


async def test_falls_back_to_parsing_the_text_when_parsed_is_missing():
    client, _ = client_with(reply(parsed=None, text='{"text": "from text"}'))
    assert (await call(GeminiLLM(client))).parsed.text == "from text"


async def test_invalid_output_is_retried_once_and_usage_is_summed():
    client, models = client_with(
        reply(parsed=None, text="not json", usage=meta(100, 10)),
        reply(parsed=Answer(text="ok"), usage=meta(100, 10)),
    )
    result = await call(GeminiLLM(client))
    assert result.parsed.text == "ok"
    assert len(models.calls) == 2
    assert result.usage.input_tokens == 200


async def test_invalid_output_keeps_being_retried_then_raises():
    client, models = client_with(*[reply(text="bad")] * 4)
    with pytest.raises(LLMError) as err:
        await call(GeminiLLM(client, retry_delay_s=0))
    assert err.value.kind == "invalid_output" and len(models.calls) == 4


def quota():
    return genai_errors.APIError(
        429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    )


async def test_a_quota_error_is_retried_until_it_clears():
    client, models = client_with(quota(), quota(), reply(parsed=Answer(text="ok")))
    result = await call(GeminiLLM(client, retry_delay_s=0))
    assert result.parsed.text == "ok" and len(models.calls) == 3


async def test_api_errors_are_mapped_after_the_retries_run_out():
    client, models = client_with(*[quota()] * 4)
    with pytest.raises(LLMError) as err:
        await call(GeminiLLM(client, retry_delay_s=0))
    assert err.value.kind == "api" and "429" in err.value.message and len(models.calls) == 4


async def test_a_request_that_can_never_work_is_not_retried():
    bad = genai_errors.APIError(400, {"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})
    client, models = client_with(bad)
    with pytest.raises(LLMError) as err:
        await call(GeminiLLM(client, retry_delay_s=0))
    assert err.value.kind == "api" and len(models.calls) == 1


async def test_timeouts_are_retried_once_then_mapped():
    async def slow():
        await asyncio.sleep(1)

    client, models = client_with(slow, slow, slow)
    with pytest.raises(LLMError) as err:
        await call(GeminiLLM(client, timeout_s=0.01, retry_delay_s=0))
    assert err.value.kind == "timeout" and len(models.calls) == 2


async def test_a_slow_call_that_works_on_the_second_try_succeeds():
    async def slow():
        await asyncio.sleep(1)

    client, _ = client_with(slow, reply(parsed=Answer(text="ok")))
    assert (await call(GeminiLLM(client, timeout_s=0.05, retry_delay_s=0))).parsed.text == "ok"


async def test_an_answer_cut_off_by_thinking_is_retried_with_more_room():
    cut = reply(parsed=None, text='{"te')
    cut.candidates = [SimpleNamespace(finish_reason=types.FinishReason.MAX_TOKENS)]
    client, models = client_with(cut, reply(parsed=Answer(text="ok")))
    cfg = StepConfig(model="gemini-2.5-pro", thinking_budget=512, max_output_tokens=512)
    result = await call(GeminiLLM(client, retry_delay_s=0), cfg=cfg)
    assert result.parsed.text == "ok"
    assert [c.config.max_output_tokens for c in models.calls] == [512, 1024]


async def test_unknown_model_costs_zero_instead_of_failing():
    client, _ = client_with(reply(parsed=Answer(text="x")))
    result = await call(GeminiLLM(client), cfg=StepConfig(model="gemini-9-ultra"))
    assert result.cost_usd == 0.0


async def test_thinking_level_and_budget_reach_the_request():
    client, models = client_with(reply(parsed=Answer(text="a")), reply(parsed=Answer(text="b")))
    llm = GeminiLLM(client)
    await call(llm, StepConfig(model="gemini-3.8-flash", thinking_level="low"))
    await call(llm, StepConfig(model="gemini-2.5-pro", thinking_budget=2048))
    assert models.calls[0].config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert models.calls[1].config.thinking_config.thinking_budget == 2048


@pytest.mark.parametrize(
    "cfg",
    [
        StepConfig(model="gemini-3.8-flash", thinking_level="extreme"),  # SDK only warns on this
        StepConfig(model="gemini-3.8-flash", thinking_level="low", thinking_budget=100),
        StepConfig(model="gemini-2.5-pro", thinking_level="low"),
        StepConfig(model="gemini-3.8-flash", thinking_budget=100),
    ],
)
async def test_bad_thinking_settings_are_configuration_errors(cfg):
    client, models = client_with(reply(parsed=Answer(text="x")))
    with pytest.raises(LLMError) as err:
        await call(GeminiLLM(client), cfg=cfg)
    assert err.value.kind == "config"
    assert models.calls == []  # nothing was sent


async def test_labels_are_only_sent_when_enabled():
    client, models = client_with(reply(parsed=Answer(text="x")), reply(parsed=Answer(text="x")))
    await call(GeminiLLM(client, send_labels=False), labels={"session_id": "abc"})
    await call(GeminiLLM(client, send_labels=True), labels={"session_id": "abc"})
    assert models.calls[0].config.labels is None
    assert models.calls[1].config.labels == {"session_id": "abc"}


async def test_fake_llm_returns_scripted_items_and_records_calls():
    fake = FakeLLM({"extraction": [Answer(text="one"), lambda user: Answer(text=user.upper())]})
    first = await fake.generate(step="extraction", cfg=CFG, system="s", user="u", schema=Answer)
    second = await fake.generate(step="extraction", cfg=CFG, system="s", user="abc", schema=Answer)
    assert (first.parsed.text, second.parsed.text) == ("one", "ABC")
    assert len(fake.calls_for("extraction")) == 2
    with pytest.raises(AssertionError):
        await fake.generate(step="extraction", cfg=CFG, system="s", user="u", schema=Answer)


class RecordingClient:
    """Stands in for genai.Client; records how the factory called it."""

    def __init__(self, *, enterprise=None, vertexai=None, api_key=None, credentials=None,
                 project=None, location=None, debug_config=None, http_options=None):  # fmt: skip
        self.kwargs = {k: v for k, v in locals().items() if k not in ("self", "debug_config")}


class OldClient:
    def __init__(self, *, vertexai=None, api_key=None, project=None, location=None,
                 http_options=None):  # fmt: skip
        self.kwargs = {k: v for k, v in locals().items() if k != "self"}


def settings(**kw):
    return Settings(_env_file=None, **kw)


def test_developer_backend_uses_the_api_key_and_retry_options(monkeypatch):
    monkeypatch.setattr(factory.genai, "Client", RecordingClient)
    client = factory.make_client(settings(llm_backend="developer", gemini_api_key="k"))
    assert client.kwargs["api_key"] == "k"
    options = client.kwargs["http_options"]
    assert options.timeout == 20_000
    assert options.retry_options.attempts == 3


def test_vertex_backend_uses_the_enterprise_flag_when_available(monkeypatch):
    monkeypatch.setattr(factory.genai, "Client", RecordingClient)
    s = settings(llm_backend="vertex", google_cloud_project="proj")
    client = factory.make_client(s)
    assert client.kwargs["enterprise"] is True
    assert (client.kwargs["project"], client.kwargs["location"]) == ("proj", "global")


def test_vertex_backend_falls_back_to_the_vertexai_flag(monkeypatch):
    monkeypatch.setattr(factory.genai, "Client", OldClient)
    client = factory.make_client(settings(llm_backend="vertex", google_cloud_project="proj"))
    assert client.kwargs["vertexai"] is True


def test_missing_credentials_are_configuration_errors():
    with pytest.raises(LLMError):
        factory.make_client(settings(llm_backend="developer"))
    with pytest.raises(LLMError):
        factory.make_client(settings(llm_backend="vertex"))


def test_vertex_with_a_missing_service_account_file_is_a_config_error(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_backend="vertex",
        google_cloud_project="p",
        google_application_credentials=str(tmp_path / "missing.json"),
    )
    with pytest.raises(LLMError) as err:
        factory.make_client(settings)
    assert err.value.kind == "config"
