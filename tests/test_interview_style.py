"""The interviewer should sound like a person: short, varied, plain, and replying to what the
user meant. Style slips are sent back to the model once, but never cost the user a turn."""

import pytest

from icp_chatbot.config import Settings
from icp_chatbot.domain.state import SessionState, TurnPair
from icp_chatbot.engine.interview import AgentTurn, InterviewEngine
from icp_chatbot.engine.style import style_problem
from icp_chatbot.enrichment.service import EnrichmentOutcome
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import Usage
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Repo
from icp_chatbot.versions import default_config

SETTINGS = Settings(_env_file=None)
T = "tenant-1"


# -- the checks ----------------------------------------------------------------------------


def test_a_short_plain_reply_passes():
    assert style_problem("So your CEO does the selling. Who gives the final yes on their side?", []) is None


def test_a_long_reply_is_too_long():
    say = " ".join(["word"] * 50) + "?"
    assert "words" in style_problem(say, [])


@pytest.mark.parametrize(
    "say",
    [
        "That makes sense. Who signs the contract?",
        "I hear you. Who signs the contract?",
        "It sounds like you are busy. Who signs the contract?",
    ],
)
def test_stock_openers_are_flagged(say):
    assert "open" in style_problem(say, [])


def test_starting_the_same_way_as_a_recent_message_is_flagged():
    recent = ["So you work with banks. Who signs?", "Fair enough. Which region?"]
    assert "same way" in style_problem("So you work with startups, too. Which one?", recent)


def test_repeating_a_phrase_from_a_recent_message_is_flagged():
    recent = ["Got it. Since you're doing outbound, who do you call first?"]
    problem = style_problem("Okay. Since you're doing outbound, which region works best?", recent)
    assert "since you're doing outbound" in problem


def test_starting_a_sentence_the_same_way_as_a_recent_message_is_flagged():
    recent = ["Got it. Since you work with both, which one comes first?"]
    assert "since you" in style_problem("Okay. Since you sell to banks, who signs?", recent)


def test_questions_may_start_with_the_same_question_word():
    recent = ["Fair enough. What do they build?"]
    assert style_problem("Right, mostly apps. What is the usual budget?", recent) is None


@pytest.mark.parametrize(
    "say",
    [
        "That is a brilliant insight. Who buys?",
        "You hit the sweet spot. Who buys?",
        "That is a great distinction. Who buys?",
        "Building AI platforms is powerful work. Who buys?",
    ],
)
def test_praise_and_idioms_are_flagged(say):
    assert "praise" in style_problem(say, [])


def test_jargon_in_quotes_is_flagged_but_contractions_are_not():
    assert "quotes" in style_problem("What is the main 'trigger' for them?", [])
    assert style_problem("You're open to any size. What's a recent deal you liked?", []) is None


def test_quoting_the_users_own_words_is_fine():
    say = 'You said "any industry". Which recent client paid best?'
    assert style_problem(say, [], user_text="any industry works for us") is None


def test_more_than_one_question_is_flagged():
    assert "one question" in style_problem("Who buys? And how long does it take?", [])


def test_heard_is_written_before_everything_else():
    assert list(AgentTurn.model_fields)[0] == "heard"


# -- in the engine -------------------------------------------------------------------------


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


def asked(question, history=()):
    return SessionState(
        pending_question=question,
        pending_kind="slot",
        questions_asked=len(history) + 1,
        history=[TurnPair(question=q, answer=a) for q, a in history],
    )


async def test_a_robotic_reply_is_sent_back_once_with_the_reason(world):
    engine, repo, llm = world
    sid = await preset(repo, asked("Who do you sell to?"))
    llm.queue(
        "interviewer",
        AgentTurn(say="That makes sense. Who signs the contract?"),
        AgentTurn(say="Got it, mostly banks. Who signs the contract there?"),
    )
    result = await engine.answer(T, sid, "mostly banks")
    assert result.question.text == "Got it, mostly banks. Who signs the contract there?"
    retry = llm.calls_for("interviewer")[1].user
    assert "rejected" in retry and "open" in retry


async def test_a_second_robotic_reply_is_used_rather_than_failing_the_turn(world):
    engine, repo, llm = world
    sid = await preset(repo, asked("Who do you sell to?"))
    llm.queue(
        "interviewer",
        AgentTurn(say="That makes sense. Who signs?"),
        AgentTurn(say="I hear you. Who signs?"),
    )
    result = await engine.answer(T, sid, "mostly banks")
    assert result.status == "active" and result.notice is None
    assert result.question.text == "I hear you. Who signs?"


async def test_if_the_rewrite_breaks_a_rule_the_first_reply_is_kept(world):
    engine, repo, llm = world
    sid = await preset(repo, asked("Who do you sell to?"))
    llm.queue(
        "interviewer",
        AgentTurn(say="That makes sense. Who signs?"),
        AgentTurn(say="All done!", action="finish", enough=False, missing=["who buys"]),
    )
    result = await engine.answer(T, sid, "mostly banks")
    assert result.status == "active" and result.notice is None
    assert result.question.text == "That makes sense. Who signs?"


async def test_a_phrase_from_the_last_few_messages_is_not_repeated(world):
    engine, repo, llm = world
    state = asked(
        "Which region works best?",
        [("Got it. Since you're doing outbound, who do you call first?", "our ceo")],
    )
    sid = await preset(repo, state)
    llm.queue(
        "interviewer",
        AgentTurn(say="Right. Since you're doing outbound, who signs?"),
        AgentTurn(say="Europe, okay. Who signs the deal there?"),
    )
    result = await engine.answer(T, sid, "europe")
    assert result.question.text == "Europe, okay. Who signs the deal there?"


async def test_the_model_is_told_which_openings_it_just_used(world):
    engine, repo, llm = world
    state = asked("To make sure I follow, which region?", [("Since you sell apps, who buys?", "startups")])
    sid = await preset(repo, state)
    llm.queue("interviewer", AgentTurn(say="Europe, okay. Who signs the deal there?"))
    await engine.answer(T, sid, "europe")
    sent = llm.calls_for("interviewer")[0].user
    assert '"since you sell"' in sent and '"to make sure"' in sent


async def test_older_messages_are_not_compared(world):
    engine, repo, llm = world
    old = [("Got it. Since you're doing outbound, who do you call first?", "our ceo")]
    filler = [("Which region?", "us"), ("How big are they?", "small"), ("Who signs?", "the owner")]
    sid = await preset(repo, asked("How long do deals take?", old + filler))
    llm.queue("interviewer", AgentTurn(say="Right. Since you're doing outbound, how long to close?"))
    result = await engine.answer(T, sid, "a month")
    assert result.question.text == "Right. Since you're doing outbound, how long to close?"


# QA 2026-10-27: phrases the v2 prompt bans that live replies still used
@pytest.mark.parametrize(
    "say",
    [
        "Fair enough. Who signs the contract?",
        "Since we know you sell to schools, who signs the contract?",
        "Given that you export, who signs the contract?",
        "That is crystal clear now. Who signs the contract?",
        "I have everything I need. Who signs the contract?",
    ],
)
def test_the_v2_stock_lines_and_lead_ins_are_caught(say):
    assert style_problem(say, []) is not None


def test_a_figure_the_user_never_gave_is_caught():
    say = "Surat has about 6-8 lakh power looms. Which cluster do you sell to first?"
    assert "figure" in style_problem(say, [], "we sell yarn to weavers in Surat")


def test_a_figure_from_the_user_or_offered_as_a_question_passes():
    assert style_problem("So 10 lakh a month in orders. Who signs off?", [], "about 10 lakh a month") is None
    assert style_problem("Is that roughly 5 crore a year in revenue?", []) is None


def test_answering_their_question_may_run_longer():
    say = " ".join(["word"] * 60) + "?"
    assert style_problem(say, [], answering=True) is None
    assert "words" in style_problem(say, [])


def test_a_plain_large_number_the_user_never_gave_is_caught():
    say = "Surat has roughly 600,000 to 800,000 power looms. Which cluster first?"
    assert "figure" in style_problem(say, [], "we sell yarn in Surat")
    assert style_problem("Founded in 2015, so who signs off?", [], "") is None
