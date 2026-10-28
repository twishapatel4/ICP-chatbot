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
import ThemeToggle from "./components/ThemeToggle";
import type { FormField, IcpResponse, Progress, StartRequest, Turn } from "./types";
import { app, chip, chipOn, chips, column, gutter, header, iconBtn, pillInput, promptActions, promptBox, promptInput, roundBtn, sendBtn } from "./ui";

type Stage = "home" | "interview" | "fallback" | "building" | "done";
interface Message {
  id: number;
  role: "bot" | "me" | "note" | "error";
  text: string;
}

type IntroKey = "company_name" | "website" | "industry" | "role";
const INTRO_EXTRAS: [IntroKey, string][] = [
  ["website", "Website (optional)"],
  ["industry", "Industry (optional)"],
  ["role", "Your role (optional)"],
];

const BUILD_NOTE = "Building your ICP. This can take a minute...";

const MSG = "max-w-[min(82%,1100px)] rounded-[22px] whitespace-pre-wrap [overflow-wrap:anywhere] ";
const MSG_CLASS: Record<Message["role"], string> = {
  bot: `${MSG}py-3 px-[18px] bg-(image:--bot-grad) self-start rounded-bl-[8px] border border-line shadow-soft`,
  me: `${MSG}py-3 px-[18px] bg-(image:--user-grad) text-user-text self-end rounded-br-[8px] shadow-soft`,
  note: `${MSG}p-0 self-center text-muted text-[12px]`,
  error: `${MSG}p-0 self-center text-warn text-[13px]`,
};

const NO_PROGRESS: Progress = { percent: 0, known: 0, total: 0 };

const STEPS: { icon: IconName; title: string; text: string; tag: string }[] = [
  { icon: "company", title: "Tell us about you", text: "Share your company name and website so I can start from what is already public.", tag: "Step 1" },
  { icon: "chat", title: "Answer a few questions", text: "A short chat about who you sell to, what they struggle with and how they buy.", tag: "Step 2" },
  { icon: "profile", title: "Review your ICP", text: "Get a profile and a scoring model you can edit, review and approve.", tag: "Step 3" },
];

const STORAGE_KEY = "icp-chat-v1";

interface Snapshot {
  stage: Stage;
  messages: Message[];
  options: string[];
  multi: boolean;
  picked: string[];
  sessionId: string | null;
  form: FormField[];
  missing: string[];
  icp: IcpResponse | null;
  approved: boolean;
  revising: boolean;
  progress?: Progress;
}

// The page can be reloaded under us (dev-server HMR reconnect, discarded background tab),
// so the chat is mirrored to sessionStorage and restored on mount.
function loadSnapshot(): Snapshot | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const snap = JSON.parse(raw) as Snapshot;
    // a "building" note left in a chat that is no longer building is stale (saved before it was cleaned up)
    if (snap.stage !== "building") snap.messages = snap.messages.filter((m) => !(m.role === "note" && m.text === BUILD_NOTE));
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
  const [stage, setStage] = useState<Stage>(["signup", "starting"].includes(saved?.stage as string) ? "home" : (saved?.stage ?? "home"));
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
  const [startError, setStartError] = useState("");
  const [intro, setIntro] = useState<Record<IntroKey, string>>({ company_name: "", website: "", industry: "", role: "" });
  const nextId = useRef(Math.max(0, ...(saved?.messages ?? []).map((m) => m.id)) + 1);
  const firstNewId = useRef(nextId.current); // messages restored after a reload show at once, not typed out

  const push = useCallback((role: Message["role"], text: string) => {
    setMessages((m) => [...m, { id: nextId.current++, role, text }]);
  }, []);

  // The profile panel beside the chat. On a phone-sized screen it covers the chat, so it starts
  // closed there and the chat, with its last message, is what they see first.
  const wide = () => window.matchMedia("(min-width: 768px)").matches;
  const [showProfile, setShowProfile] = useState(wide);

  // bot replies that have finished typing out; the options under a reply wait for it
  const [typed, setTyped] = useState<Set<number>>(new Set());
  const markTyped = useCallback((id: number) => setTyped((t) => (t.has(id) ? t : new Set(t).add(id))), []);
  const lastBot = [...messages].reverse().find((m) => m.role === "bot");
  const replyTyped = !lastBot || lastBot.id < firstNewId.current || typed.has(lastBot.id);

  useEffect(() => {
    // the bottom of the page, not of the log: the composer is sticky and would cover the last message
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
  }, [messages, stage, icp, options, replyTyped]);

  useEffect(() => {
    const snap: Snapshot = { stage, messages, options, multi, picked, sessionId, form, missing, icp, approved, revising, progress };
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snap));
    } catch {
      // storage unavailable: the chat just will not survive a reload
    }
  }, [stage, messages, options, multi, picked, sessionId, form, missing, icp, approved, revising, progress]);

  // A reload mid-build loses the in-flight request; ask for the ICP again.
  const resumed = useRef(false);
  useEffect(() => {
    if (resumed.current || !saved || saved.stage !== "building" || !saved.sessionId) return;
    resumed.current = true;
    void makeIcp(saved.sessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function newChat() {
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
    setProgress(NO_PROGRESS);
    setBusy(false);
    setIntro({ company_name: "", website: "", industry: "", role: "" });
    setStage("home");
  }

  async function makeIcp(id: string) {
    setStage("building");
    // the "building" note is removed once the profile is ready
    push("note", BUILD_NOTE);
    try {
      setIcp(await api.buildIcp(id));
      setShowProfile(wide());
      setStage("done");
    } catch (e) {
      const missing = (e instanceof api.ApiError && (e.detail as { not_enough_info?: string[] })?.not_enough_info) || null;
      if (missing) {
        push(
          "bot",
          `You have not provided enough information to build an ICP yet. Still missing: ${missing.join(", ")}. Tell me more about these and I will build it.`,
        );
        setRevising(true);
        setStage("interview");
        return;
      }
      push("error", explain(e));
      setStage("done");
    } finally {
      // the build is over, either way: the "this can take a minute" note no longer applies
      setMessages((m) => m.filter((x) => !(x.role === "note" && x.text === BUILD_NOTE)));
    }
  }

  async function showFallback(reason: string | null) {
    setOptions([]);
    push(
      reason === "model_unavailable" ? "error" : "bot",
      reason === "model_unavailable"
        ? "My AI service is not responding right now, so I cannot continue the chat. This is not your fault. You can fill in this short form instead, or try again later."
        : "I could not continue the conversation, so here is a short form instead.",
    );
    try {
      setForm(await api.getFallbackForm());
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
      push("bot", turn.question.text);
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
    setBusy(true);
    try {
      await run();
    } catch (e) {
      push("error", explain(e));
    } finally {
      setBusy(false);
    }
  }

  const start = (body: StartRequest) =>
    guarded(async () => {
      // the welcome screen stays up, showing it is working, until the first question arrives: the
      // chat then opens with that question, not with an echo of what was typed
      setStartError("");
      try {
        await handleTurn(await api.startSession(body));
      } catch (e) {
        setStartError(explain(e));
      }
    });

  // Picked chips and typed text go out as one answer.
  const send = (typed: string) => {
    const text = [...picked, typed].filter(Boolean).join(", ");
    if (!sessionId || !text) return;
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
    });

  // The current profile stays on the page (collapsed) while they explain what to change.
  function improve() {
    // from an approved profile this is "continue the conversation": the rebuilt profile needs approving again
    push("bot", approved ? "Happy to keep going. What would you like to change or add?" : "Sorry about that. What should be changed or added?");
    setApproved(false);
    setRevising(true);
    setStage("interview");
  }

  if (stage === "home") {
    return (
      <main className={app}>
        <header className={header}>
          <div className="flex items-center gap-2.5" />
          <ThemeToggle />
        </header>
        <div className="flex-1 flex flex-col items-center justify-center text-center gap-0 px-[clamp(16px,4vw,48px)] pt-0 pb-10">
          <span className="inline-flex items-center justify-center size-16 rounded-full bg-panel shadow-[0_10px_28px_rgba(20,24,31,.1),0_0_0_1px_var(--line)]">
            <Icon name="sparkle" size={26} />
          </span>
          <h1 className="mt-[26px] mx-0 mb-0 text-[clamp(26px,3.4vw,34px)] leading-[1.15] font-medium tracking-[-.025em]">
            <TextEffect preset="fade-in-blur" speedReveal={1.4}>Build your</TextEffect>{" "}
            <TextEffect preset="fade-in-blur" speedReveal={1.4} delay={0.25} className="text-muted">Ideal Customer Profile</TextEffect>
          </h1>
          <p className="text-muted text-[14px] max-w-[520px] mt-[10px] mx-0 mb-0">A short conversation about your business. At the end you get an ICP and a scoring model you can review and approve.</p>
          <div className="grid grid-cols-3 gap-[18px] mt-11 w-full max-w-[1000px] text-left max-[820px]:grid-cols-1">
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
          <form
            className="w-full max-w-[1000px] mt-14"
            onSubmit={(e) => {
              e.preventDefault();
              const body: Record<string, string> = {};
              for (const [k, v] of Object.entries(intro)) if (v.trim()) body[k] = v.trim();
              if (!body.company_name || busy) return;
              void start(body as unknown as StartRequest);
            }}
          >
            <div className={promptBox}>
              <input
                className={promptInput}
                type="text"
                maxLength={200}
                disabled={busy}
                autoFocus
                placeholder="Your company name"
                aria-label="Company name"
                value={intro.company_name}
                onChange={(e) => setIntro({ ...intro, company_name: e.target.value })}
              />
              <div className={promptActions}>
                <button className={`${roundBtn} ${sendBtn}`} type="submit" aria-label="Start" title="Start" disabled={busy || !intro.company_name.trim()}>
                  <Icon name="arrowUp" size={18} />
                </button>
              </div>
            </div>
            {startError && (
              <p className="mt-3 mx-0 mb-0 text-warn text-[14px] text-center" role="alert">
                {startError}
              </p>
            )}
            <div className="grid grid-cols-3 gap-3 mt-3 max-[720px]:grid-cols-1">
              {INTRO_EXTRAS.map(([k, label]) => (
                <input
                  className={`${pillInput} py-3 px-5 shadow-soft placeholder:text-muted`}
                  key={k}
                  type="text"
                  disabled={busy}
                  maxLength={200}
                  placeholder={label}
                  aria-label={label}
                  value={intro[k]}
                  onChange={(e) => setIntro({ ...intro, [k]: e.target.value })}
                />
              ))}
            </div>
            {busy && (
              <div className="flex items-center justify-center gap-2 mt-4 text-[16px] text-muted" role="status">
                <BotAvatar type="star" state="working" size={34} aria-hidden="true" />
                <TextShimmer>AI is generating your first question...</TextShimmer>
              </div>
            )}
          </form>
        </div>
      </main>
    );
  }

  // Once a profile exists it sits beside the chat, like an artifact, and the header toggles it.
  // On a narrow screen it covers the chat, except while they are answering the follow-up.
  const panelOpen = !!icp && showProfile;
  const profilePanel = icp && panelOpen && (
    <aside
      aria-label="Ideal Customer Profile"
      className={`border-l border-line w-[min(46%,640px)] min-w-[340px] shrink-0 max-md:fixed max-md:inset-0 max-md:z-30 max-md:w-auto max-md:min-w-0 max-md:border-l-0 max-md:bg-frame ${stage === "done" ? "" : "max-md:hidden"}`}
    >
      <div className="sticky top-[74px] max-h-[calc(100vh-74px)] overflow-y-auto p-4 flex flex-col">
        <button className={`${iconBtn} self-end mb-2`} onClick={() => setShowProfile(false)} aria-label="Hide profile" title="Hide profile">
          <Icon name="close" size={18} />
        </button>
        {stage !== "done" && <p className="mt-0 mx-0 mb-2 text-[12px] text-muted">The current profile, being updated from your answer.</p>}
        <IcpView
          icp={icp}
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
          <h1 className="sr-only">ICP Chatbot</h1>
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
      {sessionId && (stage === "interview" || stage === "building" || stage === "done") && (
        <ProgressBar progress={progress} building={stage === "building"} done={stage === "done"} />
      )}

      <div className={`${column} flex-1 flex flex-col gap-3 pt-2 pb-[18px] ${gutter}`} aria-live="polite">
        {messages.map((m, i) => (
            <InView key={m.id} className={MSG_CLASS[m.role]}>
              {m.role === "note" && stage === "building" && i === messages.length - 1 ? (
                <TextShimmer>{m.text}</TextShimmer>
              ) : m.role === "bot" && m.id >= firstNewId.current ? (
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
