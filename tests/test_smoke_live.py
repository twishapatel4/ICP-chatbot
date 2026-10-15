from icp_chatbot.cli import smoke_live
from icp_chatbot.config import Settings
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError, Usage
from icp_chatbot.versions import default_config

SETTINGS = Settings(_env_file=None)


async def test_the_smoke_test_reports_each_step_and_each_failure():
    config = default_config(SETTINGS)
    llm = FakeLLM({
        "extraction": [smoke_live.Ping(ok=True)], "seed": [smoke_live.Ping(ok=True)],
        "opener": [LLMError("config", "unknown thinking level")],
        "question": [smoke_live.Ping(ok=True)], "final_icp": [smoke_live.Ping(ok=True)],
    }, Usage(10, 2, 0, 0))  # fmt: skip
    rows = await smoke_live.run_smoke(llm, config)
    assert [r.step for r in rows] == ["extraction", "seed", "opener", "question", "final_icp"]
    assert [r.ok for r in rows] == [True, True, False, True, True]
    text = smoke_live.format_rows(rows)
    assert "FAIL opener" in text and "unknown thinking level" in text and "OK   extraction" in text
