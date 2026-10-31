"""The three-step flow: step 1 creates the session, step 2 (the profile cards) seeds it and starts
the chat, step 3 is the chat. Covers the engine (create, begin_chat, the legacy start) and the
HTTP endpoints."""

import asyncio

import pytest

import test_api
from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.slots import slot_map
from icp_chatbot.domain.state import SessionState, SlotSource, SlotValue
from icp_chatbot.engine.interview import InterviewEngine, InvalidProfile, InvalidTransition
from icp_chatbot.engine.profile_form import profile_cards
from icp_chatbot.enrichment.fetcher import FetchBlocked
from icp_chatbot.enrichment.service import EnrichmentOutcome
from icp_chatbot.linkedin.catalog import default_catalog
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.llm.types import Usage
from icp_chatbot.store import models
from icp_chatbot.store.db import init_models, make_engine, make_session_factory
from icp_chatbot.store.repo import Conflict, Repo
from test_api import SALES_B2B_PROFILE, WAV, ask, q, up

world = test_api.world  # the API fixture, shared with test_api.py

T = "tenant-1"
SALES_IDS = [c.id for c in profile_cards("sales")]
HR_IDS = [c.id for c in profile_cards("hr")]
HR_FRESHER = {
    "target_functions": ["Engineering"],
    "target_roles": ["Software engineer"],
    "experience_level": ["Fresher"],
}


class RecordingFetcher:
    """Remembers what it was asked to read, then reports the site as unreachable."""

    def __init__(self):
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        raise FetchBlocked("network", "offline in tests")


class HangingFetcher(RecordingFetcher):
    """A read that does not finish until released: the process 'dies' while it runs."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def fetch(self, url):
        self.urls.append(url)
        await self.release.wait()
        raise FetchBlocked("network", "offline in tests")


# -- step 1 and the form definition ---------------------------------------------------------------


async def test_step1_returns_cards_for_track(world):
    w = await world()
    hr = await w.create(track="hr")
    assert hr["step"] == "profile" and hr["track"] == "hr"
    assert [c["id"] for c in hr["cards"]] == HR_IDS
    assert "question" not in hr  # the first chat question comes after step 2
    cards = {c["id"]: c for c in hr["cards"]}
    assert cards["education"]["available"] is False  # no degree catalog yet: the form hides it
    assert cards["role_hiring_for"]["available"] is True

    sales = await w.create(track="sales")
    assert sales["track"] == "sales" and [c["id"] for c in sales["cards"]] == SALES_IDS
    assert all(c["available"] for c in sales["cards"])
    assert w.llm.calls_for("opener") == []

    form = await w.call("GET", "/profile-form?track=hr")
    assert form.status_code == 200 and form.json() == hr["cards"]
    state = (await w.repo.get_session(T, hr["session_id"])).state
    assert (state.step, state.track) == ("profile", "hr")


async def test_missing_track_defaults_to_sales(world):
    w = await world()
    created = await w.call("POST", "/sessions", json={"company_name": "Acme"})
    assert created.status_code == 201
    body = created.json()
    assert body["track"] == "sales" and [c["id"] for c in body["cards"]] == SALES_IDS
    assert (await w.call("GET", "/profile-form")).json() == body["cards"]
    state = (await w.repo.get_session(T, body["session_id"])).state
    assert (state.step, state.track) == ("profile", "sales")
    bad = await w.call("POST", "/sessions", json={"company_name": "Acme", "track": "pirate"})
    assert bad.status_code == 422
    assert (await w.call("GET", "/profile-form?track=pirate")).status_code == 422


async def test_cards_carry_labels_choices_and_skip_rules(world):
    w = await world()
    sales = (await w.call("GET", "/profile-form?track=sales")).json()
    fields = {f["slot"]: f for c in sales for f in c["fields"]}
    assert "business_model" not in fields and "company_size" not in fields
    industries = fields["industries"]
    assert (industries["facet"], industries["any_allowed"], industries["choices"]) == (
        "industry", True, [],
    )  # fmt: skip
    # business_model is not a form field, so the browser must not wait for an answer to it
    assert industries["depends_on"] == []
    assert fields["buyer_roles"]["depends_on"] == []
    assert fields["lead_geographies"]["depends_on"] == []
    assert fields["buyer_roles"]["optional"] is False and fields["buyer_roles"]["input"] == "title"
    assert {c["id"]: c["required"] for c in sales} == {
        c.id: c.required for c in profile_cards("sales")
    }

    hr = {f["slot"]: f for c in (await w.call("GET", "/profile-form?track=hr")).json()
          for f in c["fields"]}  # fmt: skip
    assert hr["experience_level"]["choices"] == ["Fresher", "Experienced", "Both"]
    assert hr["years_experience"]["depends_on"] == [["experience_level", "Fresher"]]
    assert "candidate_locations" not in hr and "required_skills" not in hr


# -- step 2 --------------------------------------------------------------------------------------


async def test_profile_submit_seeds_slots_and_returns_first_question(world):
    w = await world()
    sid = (await w.create())["session_id"]
    answers = {
        "industries": ["Software Development"],
        "lead_geographies": ["India"],
        "company_hq_geographies": ["Any"],
        "buyer_roles": ["Sales director"],
        "buyer_roles": ["CFO"],
    }
    w.llm.queue("opener", q("What do you sell?"))
    response = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": answers})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["question"]["kind"] == "opener" and body["question"]["text"] == "What do you sell?"
    assert (body["status"], body["questions_asked"]) == ("active", 1)
    assert body["progress"]["percent"] > 0  # step 2 answers already count

    state = (await w.repo.get_session(T, sid)).state
    assert state.step == "chat"
    industries = state.slots["industries"]
    assert industries.values == ["Software Development"] and industries.refs[0].id == "4"
    assert (industries.source, industries.evidence) == (SlotSource.STATED, "step 2 form")
    assert state.slots["company_hq_geographies"].values == ["Any"]
    assert state.slots["buyer_roles"].values == ["CFO"]

    view = (await w.call("GET", f"/sessions/{sid}")).json()
    assert view["step"] == "chat" and view["question"]["text"] == "What do you sell?"
    assert view["cards"] is None and view["answers"] is None


async def test_profile_cannot_be_submitted_twice(world):
    w = await world()
    sid = (await w.start())["session_id"]
    again = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": SALES_B2B_PROFILE})
    assert again.status_code == 409
    assert len(w.llm.calls_for("opener")) == 1


async def test_profile_missing_required_is_422(world):
    w = await world()
    sid = (await w.create())["session_id"]
    partial = {"industries": ["Not an industry"]}
    response = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": partial})
    assert response.status_code == 422
    assert response.json()["detail"]["missing_required"] == [
        "industries", "company_hq_geographies", "buyer_roles",
    ]  # fmt: skip
    malformed = await w.call(
        "POST", f"/sessions/{sid}/profile", json={"answers": {"industries": "Any"}}
    )
    assert malformed.status_code == 422
    assert "industries" in malformed.json()["detail"]["missing_required"]
    assert w.llm.calls == []
    assert (await w.repo.get_session(T, sid)).state.step == "profile"


async def test_get_session_in_profile_step_returns_cards_and_saved_answers(world):
    w = await world()
    sid = (await w.create(track="hr"))["session_id"]
    fresh = (await w.call("GET", f"/sessions/{sid}")).json()
    assert (fresh["step"], fresh["track"], fresh["status"]) == ("profile", "hr", "active")
    assert [c["id"] for c in fresh["cards"]] == HR_IDS
    assert fresh["answers"] == {} and fresh["question"] is None

    partial = {
        "target_roles": ["Software engineer", "  "],
        "experience_level": ["Fresher"],
        "not_a_slot": ["x"],
    }
    rejected = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": partial})
    assert rejected.status_code == 422
    resumed = (await w.call("GET", f"/sessions/{sid}")).json()
    assert resumed["step"] == "profile"
    assert resumed["answers"] == {
        "target_roles": ["Software engineer"],
        "experience_level": ["Fresher"],
    }


async def test_chat_endpoint_409_before_profile(world):
    w = await world()
    sid = (await w.create())["session_id"]
    for method, path, kw in (
        ("POST", "answers", {"json": {"text": "hello"}}),
        ("POST", "detail", {}),
        ("POST", "revise", {"json": {"text": "change it"}}),
        ("POST", "finish", {}),
        ("POST", "icp", {}),
    ):
        response = await w.call(method, f"/sessions/{sid}/{path}", **kw)
        assert (response.status_code, response.json()) == (
            409, {"detail": "profile_incomplete"},
        ), path  # fmt: skip
    voice = await w.client.post(f"/sessions/{sid}/transcribe", content=b"x", headers=WAV)
    assert (voice.status_code, voice.json()) == (409, {"detail": "profile_incomplete"})
    assert (await w.call("GET", f"/sessions/{sid}")).status_code == 200
    assert w.llm.calls == []


async def test_empty_answer_in_profile_step_is_409(world):
    w = await world()
    sid = (await w.create())["session_id"]
    for path in ("answers", "revise"):
        response = await w.call("POST", f"/sessions/{sid}/{path}", json={"text": ""})
        assert (response.status_code, response.json()) == (
            409, {"detail": "profile_incomplete"},
        ), path  # fmt: skip
    assert w.llm.calls == []


async def test_fallback_in_profile_step_sets_chat_and_completes(world):
    w = await world()
    sid = (await w.create())["session_id"]
    missing = (await w.call("POST", f"/sessions/{sid}/fallback", json={"answers": {}})).json()
    answers = {key: [f"value for {key}"] for key in missing["detail"]["missing_required"]}
    done = await w.call("POST", f"/sessions/{sid}/fallback", json={"answers": answers})
    assert done.status_code == 200 and done.json()["icp_ready"]
    record = await w.repo.get_session(T, sid)
    assert (record.state.step, record.status) == ("chat", "completed")
    view = (await w.call("GET", f"/sessions/{sid}")).json()
    assert (view["step"], view["status"]) == ("chat", "completed")
    later = await w.call(
        "POST", f"/sessions/{sid}/profile", json={"answers": SALES_B2B_PROFILE}
    )  # the fallback closed the session: the cards cannot be submitted afterwards
    assert later.status_code == 409 and later.json() == {"detail": "session_closed"}
    assert w.llm.calls == []


async def test_profile_and_session_get_are_tenant_scoped(world):
    w = await world()
    sid = (await w.create())["session_id"]
    other = await w.call(
        "POST", f"/sessions/{sid}/profile", tenant="tenant-2", json={"answers": SALES_B2B_PROFILE}
    )
    assert other.status_code == 404
    assert (await w.call("GET", f"/sessions/{sid}", tenant="tenant-2")).status_code == 404
    assert w.llm.calls == []
    assert (await w.repo.get_session(T, sid)).state.step == "profile"


async def test_old_session_without_step_still_answers(world):
    w = await world()
    await w.repo.ensure_tenant(T)
    config = await w.app.state.container.engine.ensure_live_config()
    state = SessionState(
        pending_question="What do you sell?", pending_kind="opener", questions_asked=1
    )
    sid = await w.repo.create_session(T, config.version, state)
    async with make_session_factory(w.db)() as s, s.begin():  # saved before steps existed
        row = await s.get(models.InterviewSession, sid)
        raw = dict(row.state)
        for key in ("step", "track", "signup", "profile_answers"):
            raw.pop(key, None)
        row.state = raw

    view = (await w.call("GET", f"/sessions/{sid}")).json()
    assert (view["step"], view["track"]) == ("chat", "sales")
    assert view["cards"] is None and view["question"]["text"] == "What do you sell?"
    w.llm.queue("interviewer", ask("Who buys it?", up("product", ["Payroll"])))
    answered = await w.call("POST", f"/sessions/{sid}/answers", json={"text": "Payroll"})
    assert answered.status_code == 200 and answered.json()["question"]["text"] == "Who buys it?"


# -- options --------------------------------------------------------------------------------------


async def test_options_endpoint_search_industry(world):
    w = await world()
    found = await w.call("GET", "/options/industry?q=soft")
    assert found.status_code == 200
    items = found.json()
    assert items[0] == {
        "id": "4", "label": "Software Development", "level": 3,
        "parent": "Technology, Information and Internet", "selectable": True,
        "match": ["AI", "Artificial Intelligence", "Generative AI", "LLM", "NLP", "Computer Vision"],
    }  # fmt: skip
    assert all("soft" in i["label"].lower() for i in items)
    assert (await w.call("GET", "/options/industry?q=soft&limit=2")).json() == items[:2]
    everything = (await w.call("GET", "/options/industry")).json()
    assert [i["id"] for i in everything] == [o.id for o in default_catalog().options("industry")]
    sizes = (await w.call("GET", "/options/company_size")).json()
    assert [s["id"] for s in sizes] == list("ABCDEFGHI")


async def test_options_unknown_facet_404(world):
    w = await world()
    assert (await w.call("GET", "/options/colour")).status_code == 404
    degree = await w.call("GET", "/options/degree")  # a known facet without a catalog yet
    assert (degree.status_code, degree.json()) == (200, [])
    assert (await w.call("GET", "/options/industry?limit=1001")).status_code == 422
    assert (await w.call("GET", "/options/industry?limit=0")).status_code == 422
    assert len((await w.call("GET", "/options/industry?limit=1000")).json()) == len(
        default_catalog().options("industry")
    )
    assert (await w.call("GET", "/options/industry", tenant=None)).status_code == 401


# -- tracks and the fallback form ------------------------------------------------------------------


async def test_hr_track_session_flow(world):
    w = await world()
    created = await w.create(track="hr", role=None)
    sid = created["session_id"]
    assert {c["id"]: c["available"] for c in created["cards"]}["education"] is False

    experienced = {**HR_FRESHER, "experience_level": ["Experienced"]}
    gap = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": experienced})
    assert gap.status_code == 422
    assert gap.json()["detail"]["missing_required"] == [
        "years_experience", "current_industries", "current_company_size",
    ]  # fmt: skip

    # a fresher needs the education card, which is unavailable, so it is not required
    w.llm.queue("opener", q("Which tools should they know?"))
    started = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": HR_FRESHER})
    assert started.status_code == 200, started.text
    assert started.json()["question"]["kind"] == "opener"
    state = (await w.repo.get_session(T, sid)).state
    assert (state.track, state.step) == ("hr", "chat")
    assert state.slots["target_functions"].refs[0].facet == "function"
    assert "education_level" not in state.slots and "years_experience" not in state.slots
    view = (await w.call("GET", f"/sessions/{sid}")).json()
    assert (view["track"], view["step"]) == ("hr", "chat")


async def test_hr_profile_with_stale_skills_and_location_keys_is_accepted_and_ignored(world):
    w = await world()
    sid = (await w.create(track="hr", role=None))["session_id"]
    stale = {**HR_FRESHER, "required_skills": ["Python"], "candidate_locations": ["India"]}
    w.llm.queue("opener", q("Which skills must they have?"))
    started = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": stale})
    assert started.status_code == 200, started.text
    state = (await w.repo.get_session(T, sid)).state
    assert not {"required_skills", "candidate_locations"} & set(state.profile_answers)
    assert not {"required_skills", "candidate_locations"} & set(state.slots)


async def test_fallback_form_uses_session_track(world):
    w = await world()
    hr_keys = [f["key"] for f in (await w.call("GET", "/fallback-form?track=hr")).json()]
    sales_keys = [f["key"] for f in (await w.call("GET", "/fallback-form")).json()]
    assert "target_roles" in hr_keys and "product" not in hr_keys
    assert "product" in sales_keys and "target_roles" not in sales_keys
    assert (await w.call("GET", "/fallback-form?track=pirate")).status_code == 422

    sid = (await w.start(track="hr", profile=HR_FRESHER))["session_id"]
    partial = await w.call("POST", f"/sessions/{sid}/fallback", json={"answers": {}})
    assert partial.status_code == 422
    missing = partial.json()["detail"]["missing_required"]
    assert missing and set(missing) <= set(hr_keys)
    answers = {key: [f"value for {key}"] for key in missing}
    done = await w.call("POST", f"/sessions/{sid}/fallback", json={"answers": answers})
    assert done.status_code == 200 and done.json()["icp_ready"]


# -- restarts and the background website read ------------------------------------------------------


async def test_begin_chat_after_engine_restart_uses_persisted_signup(world):
    first_fetcher = HangingFetcher()
    first = await world(fetcher=first_fetcher)
    created = await first.create(website="acme.test", industry="Software Development")
    sid = created["session_id"]

    # the process went away while its website read was still running; another one serves step 2
    second_fetcher = RecordingFetcher()
    second = await world(db=first.db, fetcher=second_fetcher)
    second.llm.queue("opener", q("What do you sell?"))
    response = await second.call(
        "POST", f"/sessions/{sid}/profile", json={"answers": SALES_B2B_PROFILE}
    )
    assert response.status_code == 200, response.text
    assert second_fetcher.urls == ["https://acme.test"]  # read again, from the saved signup
    prompt = second.llm.calls_for("opener")[0].user
    assert "Company: Acme" in prompt and "CTO" in prompt and "Software Development" in prompt
    state = (await second.repo.get_session(T, sid)).state
    assert state.slots["online_presence"].values == ["acme.test"]
    first_fetcher.release.set()


async def test_begin_chat_after_restart_reuses_the_finished_read(world):
    first_fetcher = RecordingFetcher()
    first = await world(fetcher=first_fetcher)
    sid = (await first.create(website="acme.test"))["session_id"]
    await first.app.state.container.engine.drain()  # the read finished before the restart

    second_fetcher = RecordingFetcher()
    second = await world(db=first.db, fetcher=second_fetcher)
    second.llm.queue("opener", q("What do you sell?"))
    response = await second.call(
        "POST", f"/sessions/{sid}/profile", json={"answers": SALES_B2B_PROFILE}
    )
    assert response.status_code == 200
    assert first_fetcher.urls == ["https://acme.test"] and second_fetcher.urls == []
    assert [r.outcome for r in await second.repo.list_enrichment(T, sid)] == ["blocked"]
    assert "could not be read" in second.llm.calls_for("opener")[0].user


# -- the engine ------------------------------------------------------------------------------------


class FakeEnrichment:
    def __init__(self, outcome=None):
        self.outcome = outcome or EnrichmentOutcome("skipped")
        self.calls = 0
        self.gate: asyncio.Event | None = None  # set: the read waits until it is released

    async def run(self, signup, config, labels=None):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.outcome


class CountingGuard:
    def __init__(self):
        self.calls = 0

    async def check_start(self, tenant_id):
        self.calls += 1


@pytest.fixture
async def eng():
    async def build(**settings):
        db = make_engine("sqlite+aiosqlite:///:memory:")
        await init_models(db)
        repo = Repo(make_session_factory(db))
        llm = FakeLLM({}, Usage(100, 10, 0, 0))
        enrichment = FakeEnrichment()
        guard = CountingGuard()
        engine = InterviewEngine(
            repo=repo, llm=llm, enrichment=enrichment,
            settings=Settings(_env_file=None, **settings), guard=guard,
        )  # fmt: skip
        built.append((engine, db))
        return engine, repo, llm, enrichment, guard

    built = []
    yield build
    for engine, db in built:
        await engine.drain()
        await db.dispose()


def payroll_read(**kw):
    seed = {"product": SlotValue(values=["Payroll"], confidence=0.5, source=SlotSource.INFERRED)}
    return EnrichmentOutcome(
        "ok", website="https://acme.test", pages=["https://acme.test"], seed=seed, **kw
    )


async def test_legacy_start_wrapper_still_works(eng):
    engine, repo, llm, enrichment, guard = await eng()
    llm.queue("opener", q("What do you sell, and to whom?"))
    signup = SignupInfo("Acme", website="acme.test", email="jane@acme.test", role="CTO")
    result = await engine.start(T, signup)
    assert result.question.kind == "opener" and result.status == "active"
    assert result.questions_asked == 1 and guard.calls == 1 and enrichment.calls == 1
    record = await repo.get_session(T, result.session_id)
    assert (record.state.step, record.state.track) == ("chat", "sales")
    assert record.state.pending_question == "What do you sell, and to whom?"
    assert record.state.signup["company_name"] == "Acme"
    assert record.state.signup["email"] == "@acme.test"  # only the domain is kept
    assert record.usage.input_tokens == 100


async def test_create_runs_the_guard_and_reads_the_site_in_the_background(eng):
    engine, repo, llm, enrichment, guard = await eng()
    enrichment.outcome = payroll_read(usage=Usage(500, 50, 0, 0), cost_usd=0.01)
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"), track="sales")
    assert guard.calls == 1
    await engine.drain()
    record = await repo.get_session(T, sid)
    assert (record.state.step, record.state.track) == ("profile", "sales")
    assert record.usage.input_tokens == 500  # the read is paid for once, when it happens
    assert "product" not in record.state.slots  # seeded when the chat starts

    llm.queue("opener", q("You sell payroll, right?"))
    result = await engine.begin_chat(T, sid, SALES_B2B_PROFILE)
    assert result.question.text == "You sell payroll, right?"
    assert guard.calls == 1 and enrichment.calls == 1
    record = await repo.get_session(T, sid)
    assert record.state.slots["product"].source == SlotSource.INFERRED
    assert record.state.slots["business_model"].values == ["B2B"]
    assert record.state.slots["business_model"].source == SlotSource.DEFAULT
    assert record.usage.input_tokens == 600  # the read plus the opener, nothing twice
    assert "Payroll" in llm.calls_for("opener")[0].user
    assert len(await repo.list_enrichment(T, sid)) == 1


async def test_begin_chat_waits_for_a_read_still_running(eng):
    engine, repo, llm, enrichment, _ = await eng()
    enrichment.outcome = payroll_read()
    enrichment.gate = asyncio.Event()
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"))
    llm.queue("opener", q("You sell payroll?"))
    chat = asyncio.create_task(engine.begin_chat(T, sid, SALES_B2B_PROFILE))
    try:
        await asyncio.sleep(0.01)
        assert not chat.done()
    finally:  # a failed assertion must not leave the read (and drain) waiting forever
        enrichment.gate.set()
    await chat
    assert enrichment.calls == 1
    assert (await repo.get_session(T, sid)).state.slots["product"].values == ["Payroll"]


async def test_a_slow_read_does_not_hold_up_the_chat(eng):
    engine, repo, llm, enrichment, _ = await eng(enrichment_timeout_seconds=0.05)
    enrichment.outcome = payroll_read()
    enrichment.gate = asyncio.Event()
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"))
    llm.queue("opener", q("What do you sell?"))
    try:
        result = await engine.begin_chat(T, sid, SALES_B2B_PROFILE)
        assert result.question.text == "What do you sell?"
        assert enrichment.calls == 1  # not started a second time
        assert "could not be read" in llm.calls_for("opener")[0].user
        assert "product" not in (await repo.get_session(T, sid)).state.slots
    finally:
        enrichment.gate.set()


async def test_double_profile_submit_counts_loser_usage(eng):
    engine, repo, llm, enrichment, _ = await eng()
    enrichment.gate = asyncio.Event()
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"))
    llm.queue("opener", q("First?"))
    llm.queue("opener", q("Second?"))
    runs = [asyncio.create_task(engine.begin_chat(T, sid, SALES_B2B_PROFILE)) for _ in range(2)]
    try:
        await asyncio.sleep(0.01)
    finally:
        enrichment.gate.set()
    results = await asyncio.gather(*runs, return_exceptions=True)
    assert sum(isinstance(r, Conflict) for r in results) == 1
    assert len(llm.calls_for("opener")) == 2
    record = await repo.get_session(T, sid)
    assert record.usage.input_tokens == 200  # both openers were paid for
    assert record.state.step == "chat"


async def test_hr_website_read_feeds_the_opener_only(eng):
    engine, repo, llm, enrichment, _ = await eng()
    enrichment.outcome = payroll_read()
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"), track="hr")
    llm.queue("opener", q("Which role are you hiring for?"))
    await engine.begin_chat(T, sid, HR_FRESHER)
    state = (await repo.get_session(T, sid)).state
    assert "product" not in state.slots and "online_presence" not in state.slots
    assert set(state.slots) <= set(slot_map("hr"))
    assert "product" in state.seed  # kept apart; its Sales-keyed lines are not sent to the opener
    assert "Payroll" not in llm.calls_for("opener")[0].user


async def test_begin_chat_rejects_invalid_answers_and_keeps_them(eng):
    engine, repo, llm, _, _ = await eng()
    sid = await engine.create(T, SignupInfo("Acme"))
    with pytest.raises(InvalidProfile) as caught:
        await engine.begin_chat(T, sid, {"lead_geographies": ["India"]})
    assert caught.value.missing == ["industries", "company_hq_geographies", "buyer_roles"]
    state = (await repo.get_session(T, sid)).state
    assert state.step == "profile" and state.profile_answers == {"lead_geographies": ["India"]}
    assert llm.calls == []
    llm.queue("opener", q("What do you sell?"))
    answers = SALES_B2B_PROFILE
    await engine.begin_chat(T, sid, answers)
    with pytest.raises(InvalidTransition):
        await engine.begin_chat(T, sid, answers)


async def test_engine_uses_hr_prompts_for_hr_session(world):
    from icp_chatbot.domain.slots import render_slot_catalog
    from icp_chatbot.prompts import prompts_for

    w = await world()
    sid = (await w.create(track="hr", role=None))["session_id"]
    w.llm.queue("opener", q("Which tools should they know?"))
    started = await w.call("POST", f"/sessions/{sid}/profile", json={"answers": HR_FRESHER})
    assert started.status_code == 200, started.text
    assert w.llm.calls_for("opener")[0].system == prompts_for("hr")["opener"]
    w.llm.queue("interviewer", ask("Got it. Which city?", up("required_skills", ["Python"])))
    reply = await w.call("POST", f"/sessions/{sid}/answers", json={"text": "Python mostly"})
    assert reply.status_code == 200, reply.text
    system = w.llm.calls_for("interviewer")[0].system
    assert system == prompts_for("hr")["interviewer"] + render_slot_catalog("hr")
    assert "ICP" not in system

    sales = await w.start()
    w.llm.queue("interviewer", ask("Who buys?", up("product", ["Payroll"])))
    await w.call("POST", f"/sessions/{sales['session_id']}/answers", json={"text": "payroll"})
    assert w.llm.calls_for("interviewer")[-1].system == (
        prompts_for("sales")["interviewer"] + render_slot_catalog("sales")
    )


async def test_api_approve_hr_document_passes_track(world):
    w = await world()
    sid = (await w.start(track="hr", profile=HR_FRESHER))["session_id"]
    partial = await w.call("POST", f"/sessions/{sid}/fallback", json={"answers": {}})
    missing = partial.json()["detail"]["missing_required"]
    done = await w.call(
        "POST", f"/sessions/{sid}/fallback", json={"answers": {k: [f"value {k}"] for k in missing}}
    )
    assert done.status_code == 200 and done.json()["icp_ready"]

    from icp_chatbot.icp.builder import ICPSynthesis

    w.llm.queue("final_icp", ICPSynthesis())
    built = await w.call("POST", f"/sessions/{sid}/icp")
    assert built.status_code == 200, built.text
    assert built.json()["document"]["track"] == "hr"
    assert built.json()["document"]["summary"].startswith("Ideal candidate:")
    approved = await w.call("POST", f"/sessions/{sid}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["document"]["track"] == "hr"


async def test_profile_form_endpoint_returns_three_sales_cards(world):
    w = await world()
    cards = (await w.call("GET", "/profile-form?track=sales")).json()
    assert [c["id"] for c in cards] == ["target_industries", "geography", "buyer_roles"]
    slots = [f["slot"] for c in cards for f in c["fields"]]
    assert slots == [
        "industries", "company_hq_geographies", "lead_geographies", "buyer_roles",
    ]  # fmt: skip
    # no field waits on a slot the form does not ask
    assert all(f["depends_on"] == [] for c in cards for f in c["fields"])


async def test_b2b_default_session_still_reaches_the_floor_with_company_size_asked_in_chat(eng):
    from icp_chatbot.engine.planner import icp_floor_met, missing_core_slots

    engine, repo, llm, _, _ = await eng()
    sid = await engine.create(T, SignupInfo("Acme"))
    llm.queue("opener", q("What do you sell?"))
    await engine.begin_chat(T, sid, SALES_B2B_PROFILE)
    state = (await repo.get_session(T, sid)).state
    assert state.slots["business_model"].source == SlotSource.DEFAULT
    assert "company_size" not in state.slots
    assert "company_size" in [s.key for s in missing_core_slots(state)]  # still asked in the chat
    assert not icp_floor_met(state)  # nothing told about the product yet
    state.slots["product"] = SlotValue(
        values=["Payroll"], confidence=0.9, source=SlotSource.STATED, evidence="chat"
    )
    assert icp_floor_met(state)  # industries, geography and departments are the customer facts


# -- website-inferred business model, and where the read runs ---------------------------------


def read_with(seed_extra: dict, **kw):
    seed = {"product": SlotValue(values=["Payroll"], confidence=0.5, source=SlotSource.INFERRED)}
    seed.update(seed_extra)
    return EnrichmentOutcome(
        "ok", website="https://acme.test", pages=["https://acme.test"], seed=seed, **kw
    )


def inferred_b2c():
    return {
        "business_model": SlotValue(
            values=["B2C"], confidence=0.6, source=SlotSource.INFERRED, evidence="site"
        )
    }


async def test_site_saying_consumers_makes_business_model_inferred_b2c(eng):
    engine, repo, llm, enrichment, _ = await eng()
    enrichment.outcome = read_with(inferred_b2c())
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"))
    llm.queue("opener", q("You sell to consumers, right?"))
    await engine.begin_chat(T, sid, SALES_B2B_PROFILE)
    state = (await repo.get_session(T, sid)).state
    model = state.slots["business_model"]
    assert (model.values, model.source) == (["B2C"], SlotSource.INFERRED)
    from icp_chatbot.engine.interview import build_message

    assert "business_model" in build_message(state, "hi", limit=10)  # in the confirm list
    guess_line = [ln for ln in build_message(state, "hi", limit=10).splitlines() if "Guessed" in ln]
    assert guess_line and "business_model" in guess_line[0]


async def test_site_without_business_model_signal_keeps_default_b2b(eng):
    engine, repo, llm, enrichment, _ = await eng()
    enrichment.outcome = payroll_read()
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"))
    llm.queue("opener", q("What do you sell?"))
    await engine.begin_chat(T, sid, SALES_B2B_PROFILE)
    model = (await repo.get_session(T, sid)).state.slots["business_model"]
    assert (model.values, model.source) == (["B2B"], SlotSource.DEFAULT)


async def test_a_stated_business_model_is_never_overwritten_by_the_site(eng):
    engine, repo, llm, enrichment, _ = await eng()
    enrichment.outcome = read_with(inferred_b2c())
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"))
    record = await repo.get_session(T, sid)
    record.state.slots["business_model"] = SlotValue(
        values=["B2B"], confidence=0.9, source=SlotSource.STATED, evidence="said so"
    )
    await repo.save_state(T, sid, record.state, expected_version=record.row_version)
    llm.queue("opener", q("What do you sell?"))
    await engine.begin_chat(T, sid, SALES_B2B_PROFILE)
    model = (await repo.get_session(T, sid)).state.slots["business_model"]
    assert (model.values, model.source) == (["B2B"], SlotSource.STATED)


async def test_inline_mode_reads_the_site_once_at_step_2_and_counts_the_spend_once(eng):
    engine, repo, llm, enrichment, _ = await eng(background_enrichment=False)
    enrichment.outcome = payroll_read(usage=Usage(500, 50, 0, 0), cost_usd=0.01)
    sid = await engine.create(T, SignupInfo("Acme", website="acme.test"))
    assert enrichment.calls == 0 and engine._reads == {}  # nothing runs after the response
    llm.queue("opener", q("You sell payroll, right?"))
    await engine.begin_chat(T, sid, SALES_B2B_PROFILE)
    assert enrichment.calls == 1
    record = await repo.get_session(T, sid)
    assert record.usage.input_tokens == 600  # the read plus the opener, nothing twice
    assert record.state.slots["product"].values == ["Payroll"]
    assert len(await repo.list_enrichment(T, sid)) == 1


async def test_task_mode_still_starts_the_read_at_step_1(eng):
    engine, _, _, enrichment, _ = await eng(background_enrichment=True)
    enrichment.outcome = payroll_read()
    await engine.create(T, SignupInfo("Acme", website="acme.test"))
    await engine.drain()
    assert enrichment.calls == 1


def test_background_enrichment_defaults_off_on_vercel(monkeypatch):
    monkeypatch.delenv("BACKGROUND_ENRICHMENT", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert Settings(_env_file=None).background_enrichment is True
    monkeypatch.setenv("VERCEL", "1")
    assert Settings(_env_file=None).background_enrichment is False
    monkeypatch.setenv("BACKGROUND_ENRICHMENT", "true")
    assert Settings(_env_file=None).background_enrichment is True


async def test_hr_transcribe_uses_the_hr_prompt_and_sales_keeps_its_own(world):
    from icp_chatbot.engine.transcribe import Transcript
    from icp_chatbot.prompts import HR_TRANSCRIBE_PROMPT, TRANSCRIBE_PROMPT

    w = await world()
    hr = (await w.start(track="hr", profile=HR_FRESHER))["session_id"]
    sales = (await w.start())["session_id"]
    for sid, expected in ((hr, HR_TRANSCRIBE_PROMPT), (sales, TRANSCRIBE_PROMPT)):
        w.llm.queue("transcribe", Transcript(text="hello"))
        r = await w.client.post(
            f"/sessions/{sid}/transcribe", content=b"RIFFdata", headers=test_api.WAV
        )
        assert r.status_code == 200, r.text
        assert w.llm.calls_for("transcribe")[-1].system == expected
    other = {**test_api.WAV, "X-Tenant-Id": "tenant-2"}
    r = await w.client.post(f"/sessions/{hr}/transcribe", content=b"x", headers=other)
    assert r.status_code == 404  # tenant isolation is unchanged


async def test_hr_409_bodies_use_no_sales_vocabulary(world):
    import re

    w = await world()
    hr = (await w.start(track="hr", profile=HR_FRESHER))["session_id"]
    for path in ("detail", "revise"):
        r = await w.call("POST", f"/sessions/{hr}/{path}", json={"text": "change it"})
        assert r.status_code == 409, (path, r.text)
        assert r.json()["detail"].startswith("not_allowed:")
        assert not re.search(r"\bICP\b|\blead|\bcustomer", r.text, re.I), r.text
