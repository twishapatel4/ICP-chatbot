import httpx
import pytest

from icp_chatbot.api.app import create_app
from icp_chatbot.config import Settings
from icp_chatbot.engine.extraction import SlotUpdate
from icp_chatbot.engine.interview import AgentTurn
from icp_chatbot.engine.questions import QuestionOutput
from icp_chatbot.enrichment.fetcher import FetchBlocked
from icp_chatbot.icp.builder import ICPSynthesis
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import LLMError, Usage
from icp_chatbot.store.db import init_models, make_engine

H = {"X-Tenant-Id": "tenant-1"}


class NoFetcher:
    async def fetch(self, url):
        raise FetchBlocked("network", "offline in tests")


def ask(say, *updates, **kw):
    return AgentTurn(say=say, action="ask", updates=list(updates), **kw)


def up(slot, values, confidence=0.9):
    return SlotUpdate(slot=slot, values=values, confidence=confidence)


def q(text):
    return QuestionOutput(question=text)


class World:
    def __init__(self, app, client, llm):
        self.app, self.client, self.llm = app, client, llm
        self.repo = app.state.container.repo

    async def call(self, method, url, tenant="tenant-1", **kw):
        headers = {"X-Tenant-Id": tenant} if tenant else {}
        return await self.client.request(method, url, headers=headers, **kw)

    async def start(self, **body):
        self.llm.queue("opener", q("What do you sell, and to whom?"))
        response = await self.call(
            "POST", "/sessions", json={"company_name": "Acme", "role": "CTO", **body}
        )
        assert response.status_code == 201, response.text
        return response.json()


@pytest.fixture
async def world():
    async def build(**settings):
        db = make_engine("sqlite+aiosqlite:///:memory:")
        await init_models(db)
        llm = FakeLLM({}, Usage(100, 10, 0, 0))
        app = create_app(
            Settings(_env_file=None, **settings),
            llm=llm,
            fetcher=NoFetcher(),
            db_engine=db,
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        built.append((client, db))
        return World(app, client, llm)

    built = []
    yield build
    for client, db in built:
        await client.aclose()
        await db.dispose()


async def run_to_completion(w):
    """Start, answer twice, then finish. Returns the session id."""
    started = await w.start()
    sid = started["session_id"]
    w.llm.queue(
        "interviewer",
        ask("Payroll for banks, nice. Who are your happiest customers so far?",
             up("product", ["Payroll software"]), summary="B2B payroll"),
    )  # fmt: skip
    best = await w.call(
        "POST", f"/sessions/{sid}/answers", json={"text": "We sell payroll software"}
    )
    assert best.json()["question"]["text"].startswith("Payroll for banks")
    w.llm.queue(
        "interviewer",
        ask("Who signs off on buying this?",
             up("industries", ["Fintech"]), up("company_size", ["50-500"]),
             up("geographies", ["Europe"]), up("pain_points", ["manual payroll"]),
             up("buyer_roles", ["CFO"]), target_slot="buyer_roles", enough=True),
    )  # fmt: skip
    nxt = await w.call(
        "POST", f"/sessions/{sid}/answers", json={"text": "Banks in Europe with 50 to 500 staff"}
    )
    assert nxt.json()["question"]["slot"] is not None
    done = await w.call("POST", f"/sessions/{sid}/finish")
    assert done.json()["icp_ready"] is True
    return sid


async def test_health_needs_no_tenant(world):
    w = await world()
    assert (await w.call("GET", "/health", tenant=None)).json() == {"status": "ok"}


@pytest.mark.parametrize("tenant", [None, "", "bad tenant!", "x" * 65])
async def test_a_missing_or_invalid_tenant_header_is_unauthorized(world, tenant):
    w = await world()
    response = await w.client.post(
        "/sessions",
        json={"company_name": "A"},
        headers={"X-Tenant-Id": tenant} if tenant is not None else {},
    )
    assert response.status_code == 401


async def test_the_full_flow_from_signup_to_an_approved_icp(world):
    w = await world()
    sid = await run_to_completion(w)
    assert (await w.call("GET", f"/sessions/{sid}")).json()["status"] == "completed"

    w.llm.queue("final_icp", ICPSynthesis())
    built = await w.call("POST", f"/sessions/{sid}/icp")
    assert built.status_code == 200
    body = built.json()
    assert (body["version"], body["author"]) == (1, "bot")
    assert "Fintech" in body["document"]["summary"]
    weights = [c["weight"] for c in body["document"]["scoring_model"]["criteria"]]
    assert weights == [20, 15, 20, 15, 10, 10, 10]

    edited = await w.call(
        "PATCH",
        f"/sessions/{sid}/icp",
        json={"kind": "field", "slot": "industries", "values": ["Insurance"]},
    )
    assert edited.json()["version"] == 2 and edited.json()["author"] == "company"
    reweighted = await w.call(
        "PATCH",
        f"/sessions/{sid}/icp",
        json={"kind": "weights", "weights": {"industry_fit": 30, "geographic_fit": 0}},
    )
    assert reweighted.status_code == 200
    toggled = await w.call(
        "PATCH",
        f"/sessions/{sid}/icp",
        json={"kind": "requirement", "criterion": "industry_fit", "requirement": "required"},
    )
    assert toggled.status_code == 200
    approved = await w.call("POST", f"/sessions/{sid}/approve")
    assert approved.json()["version"] == 5
    assert all(
        e["last_verified"]
        for s in approved.json()["document"]["sections"].values()
        for e in s.values()
    )

    current = await w.call("GET", f"/sessions/{sid}/icp")
    assert current.json()["version"] == 5
    events = [e.type for e in await w.repo.list_feedback("tenant-1", sid)]
    assert events == ["field_edit", "weight_edit", "weight_edit", "requirement_toggle", "approve"]
    assert (
        await w.repo.latest_icp("tenant-1", sid, author="bot")
    ).version == 1  # the draft is kept


async def test_building_twice_returns_the_same_version(world):
    w = await world()
    sid = await run_to_completion(w)
    w.llm.queue("final_icp", ICPSynthesis())
    first = (await w.call("POST", f"/sessions/{sid}/icp")).json()
    second = (await w.call("POST", f"/sessions/{sid}/icp")).json()
    assert first["version"] == second["version"] == 1
    assert len(w.llm.calls_for("final_icp")) == 1


async def test_the_icp_is_still_produced_when_the_final_model_step_fails(world):
    w = await world()
    sid = await run_to_completion(w)
    w.llm.queue("final_icp", LLMError("timeout", "slow"))
    response = await w.call("POST", f"/sessions/{sid}/icp")
    assert response.status_code == 200
    assert any("clean-up step failed" in x for x in response.json()["document"]["open_questions"])


async def test_another_tenant_cannot_see_or_change_a_session(world):
    w = await world()
    sid = await run_to_completion(w)
    for method, url, kw in (
        ("GET", f"/sessions/{sid}", {}),
        ("POST", f"/sessions/{sid}/answers", {"json": {"text": "hi"}}),
        ("POST", f"/sessions/{sid}/icp", {}),
        ("GET", f"/sessions/{sid}/icp", {}),
        ("POST", f"/sessions/{sid}/finish", {}),
    ):
        assert (await w.call(method, url, tenant="tenant-2", **kw)).status_code == 404, url


async def test_errors_map_to_clear_statuses(world):
    w = await world()
    started = await w.start()
    sid = started["session_id"]
    too_long = await w.call("POST", f"/sessions/{sid}/answers", json={"text": "x" * 2001})
    assert too_long.status_code == 422 and "invalid_answer" in too_long.json()["detail"]
    assert (await w.call("POST", f"/sessions/{sid}/icp")).status_code == 409  # not finished yet
    assert (await w.call("POST", f"/sessions/{sid}/detail")).status_code == 409  # not complete yet
    assert (await w.call("GET", f"/sessions/{sid}/icp")).status_code == 404  # nothing built yet
    await w.call("POST", f"/sessions/{sid}/finish")
    assert (await w.call("POST", f"/sessions/{sid}/finish")).status_code == 409
    assert (
        await w.call("POST", f"/sessions/{sid}/answers", json={"text": "late"})
    ).status_code == 409
    assert (await w.call("GET", "/sessions/does-not-exist")).status_code == 404


async def test_invalid_edits_are_rejected_without_creating_a_version(world):
    w = await world()
    sid = await run_to_completion(w)
    w.llm.queue("final_icp", ICPSynthesis())
    await w.call("POST", f"/sessions/{sid}/icp")
    bad_weights = await w.call(
        "PATCH", f"/sessions/{sid}/icp", json={"kind": "weights", "weights": {"industry_fit": 99}}
    )
    unknown = await w.call(
        "PATCH",
        f"/sessions/{sid}/icp",
        json={"kind": "field", "slot": "favourite_colour", "values": ["x"]},
    )
    nonsense = await w.call("PATCH", f"/sessions/{sid}/icp", json={"kind": "teleport"})
    assert (bad_weights.status_code, unknown.status_code, nonsense.status_code) == (422, 422, 422)
    assert (await w.call("GET", f"/sessions/{sid}/icp")).json()["version"] == 1


async def test_the_daily_session_cap_returns_429_and_does_not_stop_running_sessions(world):
    w = await world(daily_session_cap_per_tenant=1)
    started = await w.start()
    w.llm.queue("opener", q("again?"))
    refused = await w.call("POST", "/sessions", json={"company_name": "B", "role": "CTO"})
    assert refused.status_code == 429 and "tenant_sessions" in refused.json()["detail"]
    w.llm.queue("interviewer", ask("Could you say a little more?"))
    ok = await w.call("POST", f"/sessions/{started['session_id']}/answers", json={"text": "hello"})
    assert ok.status_code == 200


async def test_the_rate_limit_returns_429(world):
    w = await world(requests_per_minute_per_tenant=2)
    statuses = [(await w.call("GET", "/fallback-form")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    assert (await w.call("GET", "/fallback-form", tenant="other")).status_code == 200


async def test_the_fallback_form_completes_a_session_without_the_model(world):
    w = await world()
    sid = (await w.start())["session_id"]
    form = (await w.call("GET", "/fallback-form")).json()
    required = [f["key"] for f in form if f["required"]]
    partial = await w.call(
        "POST", f"/sessions/{sid}/fallback", json={"answers": {"industries": ["Fintech"]}}
    )
    assert (
        partial.status_code == 422 and "pain_points" in partial.json()["detail"]["missing_required"]
    )
    answers = {key: [f"value for {key}"] for key in required}
    done = await w.call("POST", f"/sessions/{sid}/fallback", json={"answers": answers})
    assert done.status_code == 200 and done.json()["icp_ready"]
    w.llm.queue("final_icp", ICPSynthesis())
    icp = await w.call("POST", f"/sessions/{sid}/icp")
    assert icp.status_code == 200 and icp.json()["document"]["completeness"]["tier_a"] == 1.0
    bad = await w.call("POST", f"/sessions/{sid}/fallback", json={"answers": answers})
    assert bad.status_code == 409  # the session is closed


async def test_detail_mode_rebuild_keeps_the_companys_earlier_edits(world):
    w = await world()
    sid = await run_to_completion(w)
    w.llm.queue("final_icp", ICPSynthesis())
    await w.call("POST", f"/sessions/{sid}/icp")
    await w.call(
        "PATCH",
        f"/sessions/{sid}/icp",
        json={"kind": "field", "slot": "industries", "values": ["Insurance"]},
    )
    await w.call(
        "PATCH",
        f"/sessions/{sid}/icp",
        json={"kind": "weights", "weights": {"industry_fit": 30, "geographic_fit": 0}},
    )

    w.llm.queue("interviewer", ask("Any compliance needs?"))
    detail = await w.call("POST", f"/sessions/{sid}/detail")
    assert detail.status_code == 200 and detail.json()["mode"] == "detail"
    w.llm.queue("interviewer", ask("Switching costs?", up("compliance", ["GDPR"]), enough=True))
    await w.call("POST", f"/sessions/{sid}/answers", json={"text": "GDPR applies"})
    await w.call("POST", f"/sessions/{sid}/finish")

    w.llm.queue("final_icp", ICPSynthesis())
    rebuilt = (await w.call("POST", f"/sessions/{sid}/icp")).json()
    entries = {k: e for s in rebuilt["document"]["sections"].values() for k, e in s.items()}
    assert entries["compliance"]["values"] == ["GDPR"]  # the new detail answer is there
    assert entries["industries"]["values"] == ["Insurance"]  # the earlier edit survived
    weights = {c["key"]: c["weight"] for c in rebuilt["document"]["scoring_model"]["criteria"]}
    assert weights["industry_fit"] == 30 and weights["geographic_fit"] == 0


async def test_a_returning_user_gets_the_pending_question_back(world):
    w = await world()
    started = await w.start()
    sid = started["session_id"]
    w.llm.queue("interviewer", ask("Could you say a little more?"))
    answered = (await w.call("POST", f"/sessions/{sid}/answers", json={"text": "hello"})).json()
    resumed = (await w.call("GET", f"/sessions/{sid}")).json()
    assert resumed["status"] == "active"
    assert resumed["question"]["text"] == answered["question"]["text"]
    assert resumed["questions_asked"] == answered["questions_asked"]


async def test_unicode_answers_are_stored_and_sent_to_the_model_unchanged(world):
    w = await world()
    sid = (await w.start())["session_id"]
    text = "我们卖薪资软件 💼 — для банков, ¿entiendes?"
    w.llm.queue("interviewer", ask("Which customers have worked out best?", up("product", ["薪资软件"])))
    response = await w.call("POST", f"/sessions/{sid}/answers", json={"text": text})
    assert response.status_code == 200
    [turn] = await w.repo.list_turns("tenant-1", sid)
    assert turn.answer_text == text
    assert text in w.llm.calls_for("interviewer")[0].user


async def test_the_app_creates_its_tables_on_startup_when_asked_to():
    db = make_engine("sqlite+aiosqlite:///:memory:")
    app = create_app(
        Settings(_env_file=None, auto_create_tables=True),
        llm=FakeLLM({}),
        fetcher=NoFetcher(),
        db_engine=db,
    )
    async with app.router.lifespan_context(app):
        await app.state.container.repo.ensure_tenant("t1")  # fails if the tables were not created


async def test_suggested_options_are_multi_select_and_survive_a_reload(world):
    w = await world()
    sid = (await w.start())["session_id"]
    w.llm.queue("interviewer", ask("Which of your customers worked best?", up("product", ["Payroll"])))
    await w.call("POST", f"/sessions/{sid}/answers", json={"text": "We sell payroll"})
    w.llm.queue(
        "interviewer",
        ask("Which sectors?", up("company_size", ["11-50"]), options=["Fintech", "Retail"]),
    )
    turn = (await w.call("POST", f"/sessions/{sid}/answers", json={"text": "Small firms"})).json()[
        "question"
    ]
    assert turn["options"] and turn["multi"] is True
    view = (await w.call("GET", f"/sessions/{sid}")).json()["question"]
    assert view["options"] == turn["options"] and view["multi"] is True


async def test_verdict_is_recorded_for_the_current_icp(world):
    w = await world()
    sid = await run_to_completion(w)
    w.llm.queue("final_icp", ICPSynthesis())
    await w.call("POST", f"/sessions/{sid}/icp")
    ok = await w.call(
        "POST", f"/sessions/{sid}/verdict", json={"correct": False, "comment": " wrong size "}
    )
    assert ok.status_code == 204
    event = (await w.repo.list_feedback("tenant-1", sid))[-1]
    assert event.type == "icp_verdict" and event.old_value == 1
    assert event.new_value == {"correct": False, "comment": "wrong size"}
    other = await w.call(
        "POST", f"/sessions/{sid}/verdict", tenant="tenant-2", json={"correct": True}
    )
    assert other.status_code == 404


async def test_verdict_needs_an_icp(world):
    w = await world()
    sid = (await w.start())["session_id"]
    assert (
        await w.call("POST", f"/sessions/{sid}/verdict", json={"correct": True})
    ).status_code == 404


def test_the_turn_response_carries_the_notice():
    from icp_chatbot.api.schemas import TurnResponse
    from icp_chatbot.engine.interview import TurnResult

    result = TurnResult("s", None, "active", None, False, False, "quick", 1, notice="try again")
    assert TurnResponse.of(result).notice == "try again"


async def test_stopping_early_still_builds_a_partial_profile_that_names_what_is_missing(world):
    # QA 2026-10-27: a user who said stop got "Building your ICP" and then no profile at all
    w = await world()
    sid = (await w.start())["session_id"]
    await w.call("POST", f"/sessions/{sid}/finish")
    w.llm.queue("final_icp", ICPSynthesis())
    built = await w.call("POST", f"/sessions/{sid}/icp")
    assert built.status_code == 200
    doc = built.json()["document"]
    assert doc["partial"] is True
    assert any(q.startswith("Not yet known:") for q in doc["open_questions"])
