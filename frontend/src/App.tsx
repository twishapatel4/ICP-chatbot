import { useCallback, useEffect, useRef, useState } from "react";
import { BotAvatar } from "bot-avatars";
import * as api from "./api";
import Composer from "./components/Composer";
import FallbackForm from "./components/FallbackForm";
import Icon, { type IconName } from "./components/Icon";
import IcpView from "./components/IcpView";
import ProgressBar from "./components/ProgressBar";
import InView from "./components/InView";
import Typewriter from "./components/Typewriter";
import ScrollProgress from "./components/ScrollProgress";
import TextShimmer from "./components/TextShimmer";
import TextEffect from "./components/TextEffect";
import StepIndicator from "./components/StepIndicator";
import ScoringSetup, { type ScoringProgress } from "./components/ScoringSetup";
import ScoringSummary from "./components/ScoringSummary";
import StepOne from "./components/StepOne";
import StepTwo, { clearDraft } from "./components/StepTwo";
import ThemeToggle from "./components/ThemeToggle";
import type { Card, FormField, IcpResponse, Progress, StartRequest, Track, Turn } from "./types";
import { buildNote, profileName } from "./wording";
import { app, primaryBtn, chip, chipOn, chips, column, gutter, header, iconBtn } from "./ui";

type Stage = "step1" | "step2" | "interview" | "fallback" | "building" | "scoring" | "result" | "done";
const STAGES: Stage[] = ["step1", "step2", "interview", "fallback", "building", "scoring", "result", "done"];
interface Message {
  id: number;
  role: "bot" | "me" | "note" | "error";
  text: string;
  live?: boolean; // just written by the AI: typed out once. Fixed texts and restored history show at once
}

const MSG = "max-w-[min(82%,1100px)] rounded-[22px] whitespace-pre-wrap [overflow-wrap:anywhere] ";
const MSG_CLASS: Record<Message["role"], string> = {
  bot: `${MSG}py-3 px-[18px] bg-(image:--bot-grad) self-start rounded-bl-[8px] border border-line shadow-soft`,
  me: `${MSG}py-3 px-[18px] bg-(image:--user-grad) text-user-text self-end rounded-br-[8px] shadow-soft`,
  note: `${MSG}p-0 self-center text-muted text-[12px]`,
  error: `${MSG}p-0 self-center text-warn text-[13px]`,
};

const STEPS: { icon: IconName; title: string; text: string; tag: string }[] = [
  { icon: "company", title: "Tell us about you", text: "Pick your role and share your company, website and industry so I can start from what is already public.", tag: "Step 1" },
  { icon: "profile", title: "Set the core profile", text: "Choose from a few options such as industry, size, seniority and location.", tag: "Step 2" },
  { icon: "chat", title: "Chat and review", text: "A short chat fills in the rest. Then review and approve your profile and scoring model.", tag: "Step 3" },
];

const NO_PROGRESS: Progress = { percent: 0, known: 0, total: 0 };

const STORAGE_KEY = "icp-chat-v1";

// The chat snapshot lives in sessionStorage and goes with the tab. A session still waiting at step 2 is
// also pointed to from localStorage, so closing the tab and coming back lands on step 2 again.
const PENDING_KEY = "icp-step2-session";
const readPending = (): string | null => {
  try {
    return localStorage.getItem(PENDING_KEY);
  } catch {
    return null;
  }
};
const writePending = (id: string | null) => {
  try {
    if (id) localStorage.setItem(PENDING_KEY, id);
    else localStorage.removeItem(PENDING_KEY);
  } catch {
    // storage unavailable: step 2 just will not survive closing the tab
  }
};

const NO_SCORING: ScoringProgress = { done: false, picked: {} };

interface Snapshot {
  stage: Stage;
  messages: Message[];
  options: string[];
  multi: boolean;
  picked: string[];
  sessionId: string | null;
  track?: Track | null;
  cards?: Card[];
  form: FormField[];
  missing: string[];
  icp: IcpResponse | null;
  approved: boolean;
  revising: boolean;
  progress?: Progress;
  scoring?: ScoringProgress;
}

// The page can be reloaded under us (dev-server HMR reconnect, discarded background tab),
// so the chat is mirrored to sessionStorage and restored on mount.
function loadSnapshot(): Snapshot | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const snap = JSON.parse(raw) as Snapshot;
    // a "building" note left in a chat that is no longer building is stale (saved before it was cleaned up)
    if (snap.stage !== "building") snap.messages = snap.messages.filter((m) => !(m.role === "note" && [buildNote("sales"), buildNote("hr"), buildNote(null)].includes(m.text)));
    return snap;
  } catch {
    return null;
  }
}

function explain(e: unknown): string {
  if (!(e instanceof api.ApiError)) return "I cannot reach the server right now. Please check your connection and try again.";
  switch (e.status) {
    case 409:
      return "That answer was already received, or the chat is closed.";
    case 422:
      return `That could not be used: ${e.message}`;
    case 429:
      return "Daily or rate limit reached. Please try again later.";
    default:
      return "Something went wrong on our side, not with your answer. Please try again in a moment.";
  }
}

export default function App() {
  const [saved] = useState(loadSnapshot);
  // a stage saved by an older build ("home", "signup", ...) is not one of these: start over at step 1
  const [stage, setStage] = useState<Stage>(STAGES.includes(saved?.stage as Stage) ? (saved?.stage as Stage) : "step1");
  const [track, setTrack] = useState<Track | null>(saved?.track ?? null);
  const [cards, setCards] = useState<Card[]>(saved?.cards ?? []);
  const [savedAnswers, setSavedAnswers] = useState<Record<string, string[]> | null>(null);
  const [messages, setMessages] = useState<Message[]>(saved?.messages ?? []);
  const [options, setOptions] = useState<string[]>(saved?.options ?? []);
  const [multi, setMulti] = useState(saved?.multi ?? true);
  const [picked, setPicked] = useState<string[]>(saved?.picked ?? []);
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(saved?.sessionId ?? null);
  const [form, setForm] = useState<FormField[]>(saved?.form ?? []);
  const [missing, setMissing] = useState<string[]>(saved?.missing ?? []);
  const [icp, setIcp] = useState<IcpResponse | null>(saved?.icp ?? null);
  const [approved, setApproved] = useState(saved?.approved ?? false);
  const [revising, setRevising] = useState(saved?.revising ?? false);
  const [progress, setProgress] = useState<Progress>(saved?.progress ?? NO_PROGRESS);
  const [scoring, setScoring] = useState<ScoringProgress>(saved?.scoring ?? NO_SCORING);
  const [startError, setStartError] = useState("");
  const [step2Error, setStep2Error] = useState(""); // a failed step 2 submit that is not about a missing answer
  const [cardsError, setCardsError] = useState(false); // the step 2 cards could not be fetched on resume
  const working = useRef(false); // set at once, so a fast double click cannot start a second request
  const nextId = useRef(Math.max(0, ...(saved?.messages ?? []).map((m) => m.id)) + 1);
  const firstNewId = useRef(nextId.current); // messages restored after a reload show at once, not typed out

  const push = useCallback((role: Message["role"], text: string, live = false) => {
    setMessages((m) => [...m, { id: nextId.current++, role, text, ...(live && { live }) }]);
  }, []);

  // The profile panel beside the chat. On a phone-sized screen it covers the chat, so it starts
  // closed there and the chat, with its last message, is what they see first.
  const wide = () => window.matchMedia("(min-width: 768px)").matches;
  const [showProfile, setShowProfile] = useState(wide);

  // bot replies that have finished typing out; the options under a reply wait for it
  const [typed, setTyped] = useState<Set<number>>(new Set());
  const markTyped = useCallback((id: number) => setTyped((t) => (t.has(id) ? t : new Set(t).add(id))), []);
  const lastBot = [...messages].reverse().find((m) => m.role === "bot");
  const replyTyped = !lastBot || !lastBot.live || lastBot.id < firstNewId.current || typed.has(lastBot.id);

  useEffect(() => {
    // the forms of steps 1 and 2 start at the top, under the step indicator
    if (stage === "step1" || stage === "step2") {
      window.scrollTo({ top: 0 });
      return;
    }
    // the bottom of the page, not of the log: the composer is sticky and would cover the last message
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
  }, [messages, stage, icp, options, replyTyped]);

  useEffect(() => {
    const snap: Snapshot = { stage, messages, options, multi, picked, sessionId, track, cards, form, missing, icp, approved, revising, progress, scoring };
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snap));
    } catch {
      // storage unavailable: the chat just will not survive a reload
    }
  }, [stage, messages, options, multi, picked, sessionId, track, cards, form, missing, icp, approved, revising, progress, scoring]);

  // A reload mid-build loses the in-flight request; ask for the ICP again.
  const resumed = useRef(false);
  useEffect(() => {
    if (resumed.current || !saved || saved.stage !== "building" || !saved.sessionId) return;
    resumed.current = true;
    void makeIcp(saved.sessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The server knows whether step 2 was submitted: a session still in step 2 resumes there, with
  // the answers it kept, whatever stage the browser remembered. A session it no longer knows starts over.
  const checked = useRef(false);
  useEffect(() => {
    const id = saved?.sessionId ? (["step2", "interview", "fallback"].includes(saved.stage) ? saved.sessionId : null) : readPending();
    if (checked.current || !id) return;
    checked.current = true;
    api
      .getSession(id)
      .then((view) => {
        setTrack(view.track);
        if (view.step === "profile") {
          // never wipe the draft or the pointer here: the session is still waiting at step 2
          setSessionId(view.session_id);
          setSavedAnswers(view.answers);
          setMessages([]);
          setOptions([]);
          setStage("step2");
          if (view.cards) setCards(view.cards);
          else if (cards.length === 0) void loadCards(view.track);
        } else {
          // step 2 was already submitted (maybe in another tab): the cards are stale
          writePending(null);
          clearDraft(id);
          if (saved?.stage === "step2" || !saved?.sessionId) {
            // resume the interview where the server has it; a closed or finished session starts over
            if (view.status === "active" && view.question) {
              setSessionId(view.session_id);
              setCards([]);
              setSavedAnswers(null);
              setMessages([]);
              void handleTurn({ ...view, status: "active", icp_ready: view.has_icp, fallback_form: false, notice: null });
            } else newChat();
          }
        }
      })
      .catch((e) => {
        if (e instanceof api.ApiError && e.status === 404) {
          writePending(null);
          clearDraft(id);
          newChat();
        }
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function loadCards(t: Track) {
    setCardsError(false);
    return api.getProfileForm(t).then(setCards).catch(() => setCardsError(true));
  }

  function newChat() {
    if (sessionId) clearDraft(sessionId);
    writePending(null);
    setStep2Error("");
    setCardsError(false);
    setTrack(null);
    setCards([]);
    setSavedAnswers(null);
    setMessages([]);
    setOptions([]);
    setPicked([]);
    setSessionId(null);
    setForm([]);
    setMissing([]);
    setIcp(null);
    setShowProfile(wide());
    setApproved(false);
    setRevising(false);
    setScoring(NO_SCORING);
    setProgress(NO_PROGRESS);
    setBusy(false);
    setStage("step1");
  }

  async function makeIcp(id: string) {
    setStage("building");
    // the "building" note is removed once the profile is ready
    push("note", buildNote(track));
    try {
      setIcp(await api.buildIcp(id));
      setShowProfile(wide());
      setStage("done");
    } catch (e) {
      const missing = (e instanceof api.ApiError && (e.detail as { not_enough_info?: string[] })?.not_enough_info) || null;
      if (missing) {
        push(
          "bot",
          `You have not provided enough information to build the ${profileName(track)} yet. Still missing: ${missing.join(", ")}. Tell me more about these and I will build it.`,
        );
        setRevising(true);
        setStage("interview");
        return;
      }
      push("error", explain(e));
      setStage("done");
    } finally {
      // the build is over, either way: the "this can take a minute" note no longer applies
      setMessages((m) => m.filter((x) => !(x.role === "note" && x.text === buildNote(track))));
    }
  }

  async function showFallback(reason: string | null) {
    setOptions([]);
    push(
      reason === "model_unavailable" ? "error" : "bot",
      reason === "model_unavailable"
        ? "My AI service is not responding right now, so I cannot continue the chat. This is not your fault. You can fill in this short form instead, or try again later."
        : reason === "unreadable"
        ? "I have not been able to understand the last few messages, so I cannot continue the conversation this way. No problem, you can start a new chat whenever you like, or fill in this short form instead."
        : "I could not continue the conversation, so here is a short form instead.",
    );
    try {
      setForm(await api.getFallbackForm(track ?? "sales"));
      setStage("fallback");
    } catch (e) {
      push("error", explain(e));
    }
  }

  async function handleTurn(turn: Turn) {
    setSessionId(turn.session_id);
    // the bar only moves forward: a corrected answer can lower the score, but never the bar
    const next = turn.progress;
    if (next) setProgress((p) => ({ percent: Math.max(p.percent, next.percent), known: Math.max(p.known, next.known), total: next.total }));
    if (turn.status === "fallback" || turn.fallback_form) return showFallback(turn.stop_reason);
    if (turn.notice) push("error", turn.notice);
    if (turn.question) {
      push("bot", turn.question.text, true);
      setOptions(turn.status === "active" ? turn.question.options : []);
      setMulti(turn.question.multi);
      setPicked([]);
    }
    if (turn.status === "completed") {
      return makeIcp(turn.session_id);
    }
    setStage("interview");
  }

  async function guarded(run: () => Promise<void>) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    try {
      await run();
    } catch (e) {
      push("error", explain(e));
    } finally {
      working.current = false;
      setBusy(false);
    }
  }

  // Step 1 creates the session; the website read starts on the server while they fill in step 2.
  const start = (body: StartRequest) =>
    guarded(async () => {
      setStartError("");
      try {
        const created = await api.startSession(body);
        setSessionId(created.session_id);
        writePending(created.session_id);
        setTrack(created.track);
        setCards(created.cards);
        setSavedAnswers(null);
        setMissing([]);
        setStage("step2");
      } catch (e) {
        setStartError(explain(e));
      }
    });

  const submitProfile = (answers: Record<string, string[]>) =>
    guarded(async () => {
      if (!sessionId) return;
      setMissing([]);
      setStep2Error("");
      try {
        const turn = await api.submitProfile(sessionId, answers);
        clearDraft(sessionId);
        writePending(null);
        await handleTurn(turn);
      } catch (e) {
        const slots = e instanceof api.ApiError && (e.detail as { missing_required?: string[] })?.missing_required;
        if (slots) {
          setMissing(slots);
          return;
        }
        setStep2Error(explain(e));
      }
    });

  // Picked chips and typed text go out as one answer.
  const send = (typed: string) => {
    const text = [...picked, typed].filter(Boolean).join(", ");
    if (!sessionId || !text || working.current) return;
    setOptions([]);
    setPicked([]);
    push("me", text);
    const call = revising ? api.reviseIcp : api.sendAnswer;
    setRevising(false);
    return guarded(async () => handleTurn(await call(sessionId, text)));
  };

  const choose = (o: string, byPointer = false) => {
    if (!multi) return send(o);
    setPicked((p) => (p.includes(o) ? p.filter((x) => x !== o) : [...p, o]));
    // The clicked option keeps keyboard focus, so Enter would press it again and un-select it.
    // After a mouse or touch pick, focus goes back to the answer box, where Enter sends.
    // A keyboard pick keeps its focus so the next option can be reached with Tab.
    if (byPointer) document.querySelector<HTMLTextAreaElement>("[data-answer-box]")?.focus();
  };

  const submitForm = (answers: Record<string, string[]>) =>
    guarded(async () => {
      if (!sessionId) return;
      try {
        await api.submitFallback(sessionId, answers);
      } catch (e) {
        if (e instanceof api.ApiError && (e.detail as { missing_required?: string[] })?.missing_required) {
          const m = (e.detail as { missing_required: string[] }).missing_required;
          setMissing(m);
          push("error", "Please fill in the highlighted fields.");
          return;
        }
        throw e;
      }
      setMissing([]);
      push("bot", "Thanks!");
      await makeIcp(sessionId);
    });

  const approve = () =>
    guarded(async () => {
      if (!sessionId) return;
      setIcp(await api.approveIcp(sessionId));
      await api.sendVerdict(sessionId, true);
      setApproved(true);
      setStage("scoring");
    });

  // The current profile stays on the page (collapsed) while they explain what to change.
  function improve() {
    // from an approved profile this is "continue the conversation": the rebuilt profile needs approving again
    push("bot", approved ? "Happy to keep going. What would you like to change or add?" : "Sorry about that. What should be changed or added?");
    setApproved(false);
    setRevising(true);
    setStage("interview");
  }

  if (stage === "scoring" && icp && sessionId) {
    return (
      <main className={app}>
        <header className={header}>
          <span className="inline-flex items-center justify-center size-[42px] rounded-full bg-panel shadow-[0_0_0_1px_var(--line),0_2px_8px_rgba(20,24,31,.08)]">
            <Icon name="sparkle" size={20} />
          </span>
          <ThemeToggle />
        </header>
        <div className={`${column} flex-1 flex flex-col gap-5 pt-2 pb-10 ${gutter}`}>
          <StepIndicator current={4} />
          <div className="text-center">
            <h1 className="mt-2 mx-0 mb-0 text-[clamp(26px,3.4vw,34px)] leading-[1.15] font-medium tracking-[-.025em]">Your scoring table</h1>
          </div>
          <div className="w-full max-w-[760px] mx-auto">
            <ScoringSetup progress={scoring} onProgress={setScoring} sessionId={sessionId} icp={icp} busy={busy} onIcp={setIcp} onBack={() => setStage("result")} onDone={() => setStage("result")} />
          </div>
        </div>
      </main>
    );
  }

  // The finished result: the profile and the scoring model, with nothing left to answer.
  if (stage === "result" && icp && sessionId) {
    return (
      <main className={app}>
        <header className={header}>
          <span className="inline-flex items-center justify-center size-[42px] rounded-full bg-panel shadow-[0_0_0_1px_var(--line),0_2px_8px_rgba(20,24,31,.08)]">
            <Icon name="sparkle" size={20} />
          </span>
          <ThemeToggle />
        </header>
        <div className={`${column} flex-1 flex flex-col gap-5 pt-2 pb-10 ${gutter}`}>
          <StepIndicator current={4} done />
          <h1 className="mt-2 mx-0 mb-0 text-center text-[clamp(26px,3.4vw,34px)] leading-[1.15] font-medium tracking-[-.025em]">
            Your {profileName(track)} is ready
          </h1>
          <div className="w-full max-w-[760px] mx-auto flex flex-col gap-4">
            <IcpView icp={icp} onScoring={() => setStage("scoring")} approved={approved} busy={busy} readOnly onApprove={approve} onImprove={improve} />
            <ScoringSummary icp={icp} />
            <div className="flex flex-wrap justify-center gap-2.5">
              <button disabled={busy} onClick={() => setStage("scoring")}>
                Edit scoring
              </button>
              <button disabled={busy} onClick={improve}>
                Refine the profile
              </button>
              <button className={primaryBtn} disabled={busy} onClick={newChat}>
                <Icon name="plus" size={18} /> New chat
              </button>
            </div>
          </div>
        </div>
      </main>
    );
  }

  const stepNo = stage === "step1" ? 1 : stage === "step2" ? 2 : 3;

  if (stage === "step1" || stage === "step2") {
    return (
      <main className={app}>
        <header className={header}>
          <div className="flex items-center gap-2.5">
            <span className="inline-flex items-center justify-center size-[42px] rounded-full bg-panel shadow-[0_0_0_1px_var(--line),0_2px_8px_rgba(20,24,31,.08)]">
              <Icon name="sparkle" size={20} />
            </span>
            {stage === "step2" && (
              <button onClick={newChat} disabled={busy}>
                <Icon name="plus" size={18} /> New chat
              </button>
            )}
          </div>
          <ThemeToggle />
        </header>
        <div className={`${column} flex-1 flex flex-col gap-5 pt-2 pb-10 ${gutter}`}>
          <StepIndicator current={stepNo} />
          {stage === "step1" ? (
            <>
              <div className="text-center">
                <h1 className="mt-2 mx-0 mb-0 text-[clamp(26px,3.4vw,34px)] leading-[1.15] font-medium tracking-[-.025em]">
                  <TextEffect preset="fade-in-blur" speedReveal={1.4}>Build your</TextEffect>{" "}
                  <TextEffect preset="fade-in-blur" speedReveal={1.4} delay={0.25} className="text-muted">{profileName(track)}</TextEffect>
                </h1>
                <p className="text-muted text-[14px] max-w-[520px] mt-[10px] mx-auto mb-0">
                  {track === "hr"
                    ? "Tell us about your company, pick a few options, then a short chat. At the end you get a candidate profile and a scoring model you can review and approve."
                    : "Tell us about your company, pick a few options, then a short chat. At the end you get a profile and a scoring model you can review and approve."}
                </p>
              </div>
              <div className="grid grid-cols-3 gap-[18px] w-full text-left max-[820px]:grid-cols-1">
                {STEPS.map((step) => (
                  <div className="flex flex-col bg-card border border-line rounded-[28px] p-[18px] shadow-soft" key={step.title}>
                    <span className="inline-flex size-11 items-center justify-center rounded-[14px] bg-panel shadow-[0_0_0_1px_var(--line),0_1px_2px_rgba(20,24,31,.05)]">
                      <Icon name={step.icon} size={22} />
                    </span>
                    <h3 className="mt-[22px] mx-0 mb-[10px] text-[17px] font-semibold tracking-[-.015em] leading-[1.2]">{step.title}</h3>
                    <p className="mt-0 mx-0 mb-[18px] text-[13px] leading-[1.55] text-muted max-w-none flex-1">{step.text}</p>
                    <span className="block text-center py-[11px] px-3 rounded-full bg-panel border border-line text-[13px] font-semibold">{step.tag}</span>
                  </div>
                ))}
              </div>
              <StepOne busy={busy} status="Setting things up..." error={startError} onTrack={setTrack} onSubmit={(body) => void start(body)} />
            </>
          ) : (
            sessionId && (
              <>
                <h1 className="sr-only">Profile builder</h1>
                {cardsError && cards.length === 0 && (
                  <div className="flex items-center justify-center gap-3 text-warn text-[13px]" role="alert">
                    The questions could not be loaded.
                    <button onClick={() => track && void loadCards(track)}>Retry</button>
                  </div>
                )}
                <StepTwo
                  key={`${sessionId}-${savedAnswers ? "saved" : "none"}`}
                  sessionId={sessionId}
                  cards={cards}
                  saved={savedAnswers}
                  disabled={busy}
                  missing={missing}
                  error={step2Error}
                  status="AI is generating your first question..."
                  onSubmit={(a) => void submitProfile(a)}
                />
              </>
            )
          )}
        </div>
      </main>
    );
  }

  // Once a profile exists it sits beside the chat, like an artifact, and the header toggles it.
  // On a narrow screen it covers the chat, except while they are answering the follow-up.
  const panelOpen = !!icp && showProfile;
  const profilePanel = icp && panelOpen && (
    <aside
      aria-label={profileName(track)}
      className={`border-l border-line w-[min(46%,640px)] min-w-[340px] shrink-0 max-md:fixed max-md:inset-0 max-md:z-30 max-md:w-auto max-md:min-w-0 max-md:border-l-0 max-md:bg-frame ${stage === "done" ? "" : "max-md:hidden"}`}
    >
      <div className="sticky top-[74px] max-h-[calc(100vh-74px)] overflow-y-auto p-4 flex flex-col">
        <button className={`${iconBtn} self-end mb-2`} onClick={() => setShowProfile(false)} aria-label="Hide profile" title="Hide profile">
          <Icon name="close" size={18} />
        </button>
        {stage !== "done" && <p className="mt-0 mx-0 mb-2 text-[12px] text-muted">The current profile, being updated from your answer.</p>}
        <IcpView
          icp={icp}
          onScoring={() => setStage("scoring")}
          approved={approved}
          busy={busy || stage !== "done"}
          readOnly={stage !== "done"}
          onApprove={approve}
          onImprove={improve}
        />
      </div>
    </aside>
  );

  return (
    <main className={app}>
      <ScrollProgress />
      <header className={header}>
        <div className="flex items-center gap-2.5">
          <h1 className="sr-only">Profile builder</h1>
          <button onClick={newChat} disabled={busy}>
            <Icon name="plus" size={18} /> New chat
          </button>
        </div>
        <div className="flex items-center gap-2.5">
          {icp && (
            <button onClick={() => setShowProfile((v) => !v)} aria-pressed={panelOpen} title={panelOpen ? "Hide profile" : "Show profile"}>
              <Icon name="panel" size={18} /> Profile
            </button>
          )}
          <ThemeToggle />
        </div>
      </header>

      <div className="flex flex-1 items-stretch">
      <div className="flex-1 min-w-0 flex flex-col">
      <div className={`${column} ${gutter}`}>
        <StepIndicator current={3} done={stage === "done"} />
      </div>
      {sessionId && (stage === "interview" || stage === "building" || stage === "done") && (
        <ProgressBar progress={progress} building={stage === "building"} done={stage === "done"} />
      )}

      <div className={`${column} flex-1 flex flex-col gap-3 pt-2 pb-[18px] ${gutter}`} aria-live="polite">
        {messages.map((m, i) => (
            <InView key={m.id} className={MSG_CLASS[m.role]}>
              {m.role === "note" && stage === "building" && i === messages.length - 1 ? (
                <TextShimmer>{m.text}</TextShimmer>
              ) : m.role === "bot" && m.live && m.id >= firstNewId.current && !typed.has(m.id) ? (
                <Typewriter text={m.text} onDone={() => markTyped(m.id)} />
              ) : (
                m.text
              )}
            </InView>
        ))}

        {options.length > 0 && replyTyped && (
          <div className="flex flex-col items-end gap-2">
            <div className={chips} role="group" aria-label={multi ? "Select all that apply" : "Pick one"}>
              {options.map((o) => (
                <button
                  key={o}
                  className={`${chip} ${picked.includes(o) ? chipOn : ""}`}
                  aria-pressed={multi ? picked.includes(o) : undefined}
                  disabled={busy}
                  onClick={(e) => choose(o, e.detail > 0)}
                >
                  {multi && picked.includes(o) ? "\u2713 " : ""}
                  {o}
                </button>
              ))}
            </div>
            {multi && <span className="text-right text-[12px] text-muted">Select all that apply, then press Send. You can also type your own below.</span>}
          </div>
        )}

        {stage === "interview" && revising && icp && !busy && (
          <InView className="flex flex-col items-end gap-1.5">
            <button className={primaryBtn} onClick={approve}>
              {"\u2713"} Looks good, continue
            </button>
            <span className="text-right text-[12px] text-muted">Happy with the profile? Move on to scoring. Or type a change below.</span>
          </InView>
        )}

        {busy && stage !== "building" && <div className={`${MSG}p-0 self-start flex items-center gap-2 text-[16px] text-muted`}>
            <BotAvatar type="star" state="working" size={34} aria-hidden="true" />
            <TextShimmer>Thinking...</TextShimmer>
          </div>}

        {stage === "done" && icp && approved && (
          <InView className="self-stretch mt-5 flex flex-col items-center gap-2.5">
            <div className="flex items-center gap-4 w-full">
              <span className="h-px flex-1 bg-line" />
              <button disabled={busy} onClick={improve}>
                <Icon name="chat" size={18} /> Continue the conversation
              </button>
              <span className="h-px flex-1 bg-line" />
            </div>
            <p className="m-0 text-[12px] text-muted">Add more detail or change something, and the profile will be rebuilt.</p>
          </InView>
        )}

        {stage === "fallback" && (
          <FallbackForm fields={form} disabled={busy} missing={missing} onSubmit={submitForm} />
        )}

      </div>

      {stage === "interview" && <Composer disabled={busy} sessionId={sessionId} hasPicks={picked.length > 0} onSend={send} />}
      </div>
      {profilePanel}
      </div>
    </main>
  );
}
