import json

import pytest

from icp_chatbot.cli.transcribe_eval import error_rate, normalize, run_eval, summarize
from icp_chatbot.config import Settings
from icp_chatbot.engine.transcribe import Transcript
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError


def test_normalize_ignores_case_punctuation_and_width():
    assert normalize("  Hello,   WORLD! ") == "hello world"
    assert normalize("ＡＢＣ。") == "abc"


@pytest.mark.parametrize(
    "said,heard,chars,expected",
    [
        ("we sell payroll software", "we sell payroll software.", False, 0.0),
        ("we sell payroll software", "we sell payroll", False, 0.25),
        ("we sell payroll software", "we sell pay roll software", False, 0.5),
        ("abcd", "abxd", True, 0.25),
        ("", "", False, 0.0),
        ("", "noise", False, 1.0),
    ],
)
def test_error_rate(said, heard, chars, expected):
    assert error_rate(said, heard, chars=chars) == pytest.approx(expected)


async def test_run_eval_scores_each_clip_and_groups_the_report(tmp_path):
    (tmp_path / "a.wav").write_bytes(b"x")
    (tmp_path / "b.wav").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            [
                {"file": "a.wav", "text": "hello there", "lang": "en", "accent": "us", "tone": "calm"},
                {"file": "b.wav", "text": "bonjour", "lang": "fr", "accent": "fr", "tone": "fast"},
                {"file": "c.txt", "text": "x", "lang": "en"},
            ]
        )
    )
    llm = FakeLLM({"transcribe": [Transcript(text="Hello there."), LLMError("api", "boom")]})
    samples = await run_eval(llm, Settings(_env_file=None), manifest)
    assert [s.wer for s in samples] == [0.0, 1.0, 1.0]
    assert samples[1].error.startswith("api") and samples[2].error == "bad type"
    report = summarize(samples)
    assert "3 clips, 2 failed" in report and "by lang:" in report and "fr" in report
