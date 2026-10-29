"""Request and response bodies of the HTTP API."""

from typing import Any

from pydantic import BaseModel, Field

from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.domain.slots import Track, slot_map
from icp_chatbot.engine.fallback import FormField
from icp_chatbot.engine.interview import TurnResult
from icp_chatbot.engine.planner import Progress
from icp_chatbot.engine.profile_form import _TITLE_SLOTS, Card, CardField, profile_cards
from icp_chatbot.linkedin.catalog import default_catalog
from icp_chatbot.engine.questions import Question
from icp_chatbot.icp.document import ICPDocument
from icp_chatbot.linkedin.catalog import Option, default_catalog


class StartRequest(BaseModel):
    """Step 1. A missing track means Sales; the free-text role is still accepted."""

    track: Track = "sales"
    company_name: str = Field(min_length=1, max_length=200)
    website: str | None = Field(default=None, max_length=500)
    email: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=100)
    industry: str | None = Field(default=None, max_length=100)
    company_size: str | None = Field(default=None, max_length=50)
    source: str | None = Field(default=None, max_length=100)

    def signup(self) -> SignupInfo:
        return SignupInfo(**self.model_dump(exclude={"track"}))


class ProfileRequest(BaseModel):
    """Step 2: slot key -> chosen values. Checked by the engine, which names any bad slot
    rather than rejecting the whole body."""

    answers: dict[str, Any] = Field(default_factory=dict)


class FieldOut(BaseModel):
    slot: str
    label: str
    input: str  # single, multi, chips, range, geo or title
    facet: str | None  # options come from GET /options/{facet}
    optional: bool
    any_allowed: bool  # "Any" may be picked, on its own
    choices: list[str]  # the fixed choices of a field without a facet
    # the registry's skip rules: hidden while that slot is unanswered or holds that value
    depends_on: list[list[str]]

    @classmethod
    def of(cls, field: CardField, track: Track) -> "FieldOut":
        slot = slot_map(track)[field.slot]
        asked = {s for c in profile_cards(track) for s in c.slots}
        return cls(
            slot=field.slot,
            label=slot.label,
            input=field.input,
            facet=field.facet,
            optional=field.optional,
            any_allowed=field.any_allowed,
            choices=[] if field.facet else list(slot.choices),
            # a controlling slot the form does not ask (Sales business_model) must not hide the field
            depends_on=[[key, value] for key, value in slot.skip_if if key in asked],
        )


class CardOut(BaseModel):
    id: str
    title: str
    required: bool
    available: bool  # false while a catalog it needs is missing: the form leaves it out
    fields: list[FieldOut]

    @classmethod
    def of(cls, card: Card, track: Track) -> "CardOut":
        catalog = default_catalog()
        return cls(
            id=card.id,
            title=card.title,
            required=card.required,
            available=all(catalog.has(facet) for facet in card.requires_catalog),
            fields=[FieldOut.of(f, track) for f in card.fields],
        )


class OptionOut(BaseModel):
    id: str
    label: str
    level: int
    parent: str | None
    # False for a row that stays valid data but should not be offered as a quick-pick (the
    # "Other territories" region); pickers may hide it or show it without a "select all" action.
    selectable: bool = True

    @classmethod
    def of(cls, option: Option) -> "OptionOut":
        return cls(
            id=option.id, label=option.label, level=option.level, parent=option.parent,
            selectable=option.selectable,
        )


class SessionCreated(BaseModel):
    """Step 1 done: the session waits for the step 2 cards."""

    session_id: str
    step: str
    track: Track
    cards: list[CardOut]


class AnswerRequest(BaseModel):
    text: str  # length is checked by the engine against MAX_ANSWER_CHARS


class TranscribeResponse(BaseModel):
    text: str  # empty when nothing intelligible was said


class VerdictRequest(BaseModel):
    correct: bool  # does the company think the ICP describes its best customers?
    comment: str = Field(default="", max_length=2000)


class FormRequest(BaseModel):
    answers: dict[str, list[str]]


class ProgressOut(BaseModel):
    percent: int  # 0 to 100: how much of the ICP is gathered
    known: int  # must-ask fields settled so far
    total: int  # must-ask fields that apply to this company

    @classmethod
    def of(cls, p: Progress) -> "ProgressOut":
        return cls(percent=p.percent, known=p.known, total=p.total)


class QuestionOut(BaseModel):
    text: str
    kind: str
    slot: str | None = None
    options: list[str] = Field(default_factory=list)
    multi: bool = True  # the user may pick several options

    @classmethod
    def of(cls, q: Question | None) -> "QuestionOut | None":
        return (
            None
            if q is None
            else cls(text=q.text, kind=q.kind, slot=q.slot, options=list(q.options), multi=q.multi)
        )


class TurnResponse(BaseModel):
    session_id: str
    status: str
    mode: str
    questions_asked: int
    question: QuestionOut | None = None
    stop_reason: str | None = None
    icp_ready: bool = False
    fallback_form: bool = False
    notice: str | None = None  # a model step failed: tell the user it is not their fault
    progress: ProgressOut = ProgressOut(percent=0, known=0, total=0)

    @classmethod
    def of(cls, r: TurnResult) -> "TurnResponse":
        return cls(
            session_id=r.session_id,
            status=r.status,
            mode=r.mode,
            questions_asked=r.questions_asked,
            question=QuestionOut.of(r.question),
            stop_reason=r.stop_reason,
            icp_ready=r.icp_ready,
            fallback_form=r.fallback_form,
            notice=r.notice,
            progress=ProgressOut.of(r.progress),
        )


class SessionView(BaseModel):
    session_id: str
    status: str
    mode: str
    questions_asked: int
    question: QuestionOut | None = None
    stop_reason: str | None = None
    has_icp: bool = False
    progress: ProgressOut = ProgressOut(percent=0, known=0, total=0)
    step: str = "chat"  # profile: step 2 not yet submitted
    track: Track = "sales"
    cards: list[CardOut] | None = None  # while step is profile
    answers: dict[str, list[str]] | None = None  # while step is profile: the last submitted


class FormFieldOut(BaseModel):
    key: str
    label: str
    description: str
    choices: list[str]
    required: bool
    facet: str | None = None  # "title": pick from GET /options/title instead of typing

    @classmethod
    def of(cls, f: FormField) -> "FormFieldOut":
        return cls(
            facet="title" if f.key in _TITLE_SLOTS and default_catalog().has("title") else None,
            key=f.key,
            label=f.label,
            description=f.description,
            choices=list(f.choices),
            required=f.required,
        )


class ICPResponse(BaseModel):
    session_id: str
    version: int
    author: str
    document: ICPDocument
