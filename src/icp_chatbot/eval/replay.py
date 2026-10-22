"""Replays a scripted conversation through the real engine and scores the ICP it produces."""

from dataclasses import dataclass, field
from datetime import date

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.engine.interview import InterviewEngine
from icp_chatbot.enrichment.fetcher import FetchBlocked, FetchedPage
from icp_chatbot.enrichment.service import EnrichmentService
from icp_chatbot.eval.cases import EvalCase
from icp_chatbot.icp.builder import build_icp
from icp_chatbot.icp.document import ICPDocument
from icp_chatbot.icp.scoring import loose_equal, validate_model
from icp_chatbot.llm.types import LLM
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Repo
from icp_chatbot.versions import ConfigVersionData

TENANT = "eval"


class CaseSiteFetcher:
    """Serves the case's website text for any address; a case without one is unreachable."""

    def __init__(self, text: str | None):
        self._text = text

    async def fetch(self, url: str) -> FetchedPage:
        if self._text is None:
            raise FetchBlocked("network", "no website in this case")
        return FetchedPage(url, self._text, [], False)


@dataclass
class CaseResult:
    name: str
    tags: list[str]
    status: str
    stop_reason: str | None
    questions_asked: int
    precision: float
    recall: float
    f1: float
    violations: int  # forbidden values found in the ICP
    scoring_valid: bool
    cost_usd: float
    errors: list[str] = field(default_factory=list)


def _pairs(values_by_slot: dict[str, list[str]], slots) -> list[tuple[str, str]]:
    return [(s, v) for s in slots for v in values_by_slot.get(s, [])]


def _present(slot: str, value: str, actual: dict[str, list[str]]) -> bool:
    return any(loose_equal(value, v) for v in actual.get(slot, []))


def score_document(case: EvalCase, document: ICPDocument) -> tuple[float, float, float, int]:
    """(precision, recall, f1, violations). Precision looks only at the slots the case expects,
    so extra fields the company did not mention are not penalized."""
    actual = document.slot_values()
    expected_pairs = _pairs(case.expected, case.expected)
    found = sum(1 for s, v in expected_pairs if _present(s, v, actual))
    recall = found / len(expected_pairs) if expected_pairs else 1.0
    actual_pairs = _pairs(actual, case.expected)
    correct = sum(1 for s, v in actual_pairs if _present(s, v, case.expected))
    precision = (
        correct / len(actual_pairs) if actual_pairs else (1.0 if not expected_pairs else 0.0)
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    violations = sum(1 for s, v in _pairs(case.forbidden, case.forbidden) if _present(s, v, actual))
    return precision, recall, f1, violations


async def replay_case(
    case: EvalCase, llm: LLM, settings: Settings, config: ConfigVersionData | None = None
) -> CaseResult:
    db = make_engine("sqlite+aiosqlite:///:memory:")
    errors: list[str] = []
    try:
        await init_models(db)
        repo = Repo(make_session_factory(db))
        if config is not None:
            await repo.publish_config(config, author="eval")
        enrichment = EnrichmentService(llm, CaseSiteFetcher(case.website_text))
        engine = InterviewEngine(
            repo=repo, llm=llm, enrichment=enrichment, settings=settings, vary_order=False
        )  # a fixed order keeps runs comparable

        result = await engine.start(TENANT, SignupInfo(**case.signup))
        sid = result.session_id
        for text in case.answers:
            if result.status != "active":
                break
            result = await engine.answer(TENANT, sid, text)
        if result.status == "active":
            result = await engine.finish(TENANT, sid)
        if result.status == "completed" and case.detail_answers:
            result = await engine.request_detail(TENANT, sid)
            for text in case.detail_answers:
                if result.status != "active":
                    break
                result = await engine.answer(TENANT, sid, text)
            if result.status == "active":
                result = await engine.finish(TENANT, sid)

        record = await repo.get_session(TENANT, sid)
        active_config = config or await repo.get_config(record.config_version)
        built = await build_icp(llm, active_config, record.state, today=date(2026, 1, 1))
        precision, recall, f1, violations = score_document(case, built.document)
        return CaseResult(
            name=case.name, tags=case.tags, status=result.status, stop_reason=result.stop_reason,
            questions_asked=record.state.questions_asked, precision=precision, recall=recall,
            f1=f1, violations=violations,
            scoring_valid=not validate_model(built.document.scoring_model),
            cost_usd=record.cost_usd + built.cost_usd, errors=errors,
        )  # fmt: skip
    except Exception as exc:  # a broken case must not stop the whole run
        errors.append(f"{type(exc).__name__}: {exc}")
        return CaseResult(
            name=case.name, tags=case.tags, status="error", stop_reason=None, questions_asked=0,
            precision=0.0, recall=0.0, f1=0.0, violations=0, scoring_valid=False, cost_usd=0.0,
            errors=errors,
        )  # fmt: skip
    finally:
        await db.dispose()


async def replay_all(
    cases: list[EvalCase], llm: LLM, settings: Settings, config=None
) -> list[CaseResult]:
    """Runs cases one after another, so a real model's rate limits are respected."""
    return [await replay_case(case, llm, settings, config) for case in cases]
