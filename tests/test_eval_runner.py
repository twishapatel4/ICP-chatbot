import json

from icp_chatbot.cli import launch_check
from icp_chatbot.config import Settings
from icp_chatbot.engine.extraction import ExtractionResult, SlotUpdate
from icp_chatbot.engine.questions import QuestionOutput
from icp_chatbot.eval.runner import run
from icp_chatbot.icp.builder import ICPSynthesis
from icp_chatbot.llm.fake import FakeLLM

SETTINGS = Settings(_env_file=None)


def write_case(tmp_path, name="mini"):
    """Writes one case into <tmp_path>/cases. Results go in tmp_path itself, never in the cases
    folder, because every .json file in that folder is read as a case."""
    directory = tmp_path / "cases"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps({
        "name": name, "tags": ["clear_website"], "signup": {"company_name": "Acme"},
        "answers": ["We sell payroll to banks", "Banks in Europe"],
        "expected": {"industries": ["banks"], "geographies": ["Europe"]},
    }))  # fmt: skip


def scripted_llm(f1_good=True):
    industries = ["Banks"] if f1_good else ["Retail"]
    return FakeLLM({
        "extraction": [
            ExtractionResult(updates=[SlotUpdate(slot="product", values=["Payroll"], confidence=0.9)]),
            ExtractionResult(updates=[SlotUpdate(slot="industries", values=industries, confidence=0.9),
                                      SlotUpdate(slot="geographies", values=["Europe"], confidence=0.9)]),
        ],
        "question": [QuestionOutput(question="Pains?")],
        "final_icp": [ICPSynthesis()],
    })  # fmt: skip


async def test_the_runner_writes_results_and_exits_zero_for_a_clean_run(tmp_path, capsys):
    write_case(tmp_path)
    out = tmp_path / "run.json"
    code = await run(tmp_path / "cases", scripted_llm(), SETTINGS, out_path=out)
    assert code == 0 and "cases=1" in capsys.readouterr().out
    saved = json.loads(out.read_text())
    assert saved["summary"]["mean_f1"] == 1.0 and saved["results"][0]["name"] == "mini"


async def test_the_runner_compares_with_a_baseline_and_fails_a_worse_candidate(tmp_path, capsys):
    write_case(tmp_path)
    baseline = tmp_path / "baseline.json"
    await run(tmp_path / "cases", scripted_llm(), SETTINGS, out_path=baseline)
    worse = await run(
        tmp_path / "cases", scripted_llm(f1_good=False), SETTINGS, baseline_path=baseline
    )
    output = capsys.readouterr().out
    assert worse == 1 and "FAIL" in output and "did not improve" in output
    same = await run(tmp_path / "cases", scripted_llm(), SETTINGS, baseline_path=baseline)
    assert same == 1  # equal is not better: a candidate must beat the baseline


async def test_the_runner_fails_when_a_case_errors(tmp_path):
    write_case(tmp_path)
    assert await run(tmp_path / "cases", FakeLLM({}), SETTINGS) == 1


def test_the_launch_check_exits_nonzero_until_the_set_is_complete(capsys):
    assert launch_check.main(["eval_cases"]) == 1
    assert "launch set needs at least 30" in capsys.readouterr().out
