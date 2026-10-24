"""Most people answering are business development executives, not founders: the interviewer is
told who it is talking to, from signup or from what it worked out, and openers follow the role."""

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.state import SessionState
from icp_chatbot.engine.interview import AgentTurn, InterviewEngine
from icp_chatbot.engine.questions import (
    LEADER_ANGLES,
    OPENER_ANGLES,
    QuestionOutput,
    pick_angle,
    role_kind,
)
from icp_chatbot.enrichment.service import EnrichmentOutcome
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import Usage
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Repo
from icp_chatbot.versions import default_config

SETTINGS = Settings(_env_file=None)
T = "tenant-1"


@pytest.mark.parametrize(
    "role, kind",
    [
        ("BDE", "sales"),
        ("Business Development Executive", "sales"),
        ("Sales manager", "sales"),
        ("Co-founder", "leader"),
        ("Founder & CEO", "leader"),
        ("Engineer", None),
        (None, None),
    ],
)
def test_roles_are_sorted_into_sales_and_leaders(role, kind):
    assert role_kind(role) == kind


@pytest.mark.parametrize("role", [None, "BDE", "Engineer"])
def test_sellers_and_unknown_roles_get_seller_angles(role):
    assert pick_angle(role) in OPENER_ANGLES


def test_founders_get_leader_angles():
    assert pick_angle("Founder") in LEADER_ANGLES


class NoEnrichment:
    async def run(self, signup, config, labels=None):
        return EnrichmentOutcome("skipped")


@pytest.fixture
async def world():
    engine_db = make_engine("sqlite+aiosqlite:///:memory:")
    await init_models(engine_db)
    repo = Repo(make_session_factory(engine_db))
    llm = FakeLLM({}, Usage(100, 10, 0, 0))
    engine = InterviewEngine(repo=repo, llm=llm, enrichment=NoEnrichment(), settings=SETTINGS)
    yield engine, repo, llm
    await engine_db.dispose()


async def preset(repo, state):
    await repo.ensure_tenant(T)
    await repo.publish_config(default_config(SETTINGS), author="test")
    return await repo.create_session(T, 1, state)


def asked(question="What do you sell?"):
    return SessionState(pending_question=question, pending_kind="slot", questions_asked=1)


async def test_the_signup_role_is_kept_and_shown_to_the_interviewer(world):
    engine, repo, llm = world
    llm.queue("opener", QuestionOutput(question="Welcome, Acme! What do you sell?"))
    llm.queue("interviewer", AgentTurn(say="Payroll, okay. Who picks up your calls?"))
    started = await engine.start(T, SignupInfo("Acme", role="BDE"))
    await engine.answer(T, started.session_id, "payroll software")
    assert "Talking to: BDE" in llm.calls_for("interviewer")[0].user


async def test_the_role_is_marked_unknown_until_someone_says(world):
    engine, repo, llm = world
    sid = await preset(repo, asked())
    llm.queue("interviewer", AgentTurn(say="Payroll, okay. Who picks up your calls?"))
    await engine.answer(T, sid, "payroll software")
    assert "Talking to: not known yet" in llm.calls_for("interviewer")[0].user


async def test_the_interviewer_can_record_who_it_is_talking_to(world):
    engine, repo, llm = world
    sid = await preset(repo, asked())
    llm.queue(
        "interviewer",
        AgentTurn(say="Payroll, okay. Who picks up your calls?", speaker="Sales (BDE)"),
        AgentTurn(say="The office manager, then. Who signs?"),
    )
    await engine.answer(T, sid, "payroll software, I do the outreach")
    await engine.answer(T, sid, "office managers")
    assert "Talking to: Sales (BDE)" in llm.calls_for("interviewer")[1].user
    assert (await repo.get_session(T, sid)).state.speaker_role == "Sales (BDE)"
