"""Request and response bodies of the HTTP API."""

from pydantic import BaseModel, Field

from icp_chatbot.engine.fallback import FormField
from icp_chatbot.engine.interview import TurnResult
from icp_chatbot.engine.questions import Question
from icp_chatbot.icp.document import ICPDocument


class StartRequest(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    website: str | None = Field(default=None, max_length=500)
    email: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=100)
    industry: str | None = Field(default=None, max_length=100)
    company_size: str | None = Field(default=None, max_length=50)
    source: str | None = Field(default=None, max_length=100)


class AnswerRequest(BaseModel):
    text: str  # length is checked by the engine against MAX_ANSWER_CHARS


class FormRequest(BaseModel):
    answers: dict[str, list[str]]


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
        )


class SessionView(BaseModel):
    session_id: str
    status: str
    mode: str
    questions_asked: int
    question: QuestionOut | None = None
    stop_reason: str | None = None
    has_icp: bool = False


class FormFieldOut(BaseModel):
    key: str
    label: str
    description: str
    choices: list[str]
    required: bool

    @classmethod
    def of(cls, f: FormField) -> "FormFieldOut":
        return cls(
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
