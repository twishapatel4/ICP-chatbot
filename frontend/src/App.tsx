import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import Composer from "./components/Composer";
import FallbackForm from "./components/FallbackForm";
import Icon, { type IconName } from "./components/Icon";
import IcpView from "./components/IcpView";
import ProgressBar from "./components/ProgressBar";
import InView from "./components/InView";
import TextShimmer from "./components/TextShimmer";
import TextEffect from "./components/TextEffect";
import ThemeToggle from "./components/ThemeToggle";
import type { FormField, IcpResponse, Progress, StartRequest, Turn } from "./types";

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
    return raw ? (JSON.parse(raw) as Snapshot) : null;
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
  const [stage, setStage] = useState<Stage>((saved?.stage as string) === "signup" ? "home" : (saved?.stage ?? "home"));
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
  const [intro, setIntro] = useState<Record<IntroKey, string>>({ company_name: "", website: "", industry: "", role: "" });
  const nextId = useRef(Math.max(0, ...(saved?.messages ?? []).map((m) => m.id)) + 1);

  const push = useCallback((role: Message["role"], text: string) => {
    setMessages((m) => [...m, { id: nextId.current++, role, text }]);
  }, []);

  useEffect(() => {
    // the bottom of the page, not of the log: the composer is sticky and would cover the last message
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
  }, [messages, stage, icp, options]);

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
    setApproved(false);
    setRevising(false);
    setProgress(NO_PROGRESS);
    setBusy(false);
    setIntro({ company_name: "", website: "", industry: "", role: "" });
    setStage("home");
  }

  async function makeIcp(id: string) {
    setStage("building");
    push("note", "Building your ICP. This can take a minute...");
    try {
      setIcp(await api.buildIcp(id));
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
      push("me", body.website ? `${body.company_name} (${body.website})` : body.company_name);
      await handleTurn(await api.startSession(body));
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
    if (byPointer) document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus();
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
    setRevising(true);
    push("bot", "Sorry about that. What should be changed or added?");
    setStage("interview");
  }

  if (stage === "home") {
    return (
      <main className="app home">
        <header>
          <div className="head-left" />
          <ThemeToggle />
        </header>
        <div className="home-body">
          <span className="logo">
            <Icon name="sparkle" size={26} />
          </span>
          <h1>
            <TextEffect preset="fade-in-blur" speedReveal={1.4}>Build your</TextEffect>{" "}
            <TextEffect preset="fade-in-blur" speedReveal={1.4} delay={0.25} className="muted">Ideal Customer Profile</TextEffect>
          </h1>
          <p>A short conversation about your business. At the end you get an ICP and a scoring model you can review and approve.</p>
          <div className="steps">
            {STEPS.map((step) => (
              <div className="step" key={step.title}>
                <span className="step-icon">
                  <Icon name={step.icon} size={22} />
                </span>
                <h3>{step.title}</h3>
                <p>{step.text}</p>
                <span className="step-tag">{step.tag}</span>
              </div>
            ))}
          </div>
          <form
            className="home-composer"
            onSubmit={(e) => {
              e.preventDefault();
              const body: Record<string, string> = {};
              for (const [k, v] of Object.entries(intro)) if (v.trim()) body[k] = v.trim();
              if (!body.company_name || busy) return;
              newChat();
              void start(body as unknown as StartRequest);
            }}
          >
            <div className="prompt-box">
              <input
                type="text"
                maxLength={200}
                autoFocus
                placeholder="Your company name"
                aria-label="Company name"
                value={intro.company_name}
                onChange={(e) => setIntro({ ...intro, company_name: e.target.value })}
              />
              <div className="prompt-actions">
                <button className="send" type="submit" aria-label="Start" title="Start" disabled={!intro.company_name.trim()}>
                  <Icon name="arrowUp" size={18} />
                </button>
              </div>
            </div>
            <div className="intro-extras">
              {INTRO_EXTRAS.map(([k, label]) => (
                <input
                  key={k}
                  type="text"
                  maxLength={200}
                  placeholder={label}
                  aria-label={label}
                  value={intro[k]}
                  onChange={(e) => setIntro({ ...intro, [k]: e.target.value })}
                />
              ))}
            </div>
          </form>
        </div>
      </main>
    );
  }

  return (
    <main className="app">
      <header>
        <div className="head-left">
          <h1 className="sr-only">ICP Chatbot</h1>
          <button onClick={newChat} disabled={busy}>
            <Icon name="plus" size={18} /> New chat
          </button>
        </div>
        <ThemeToggle />
      </header>

      {sessionId && (stage === "interview" || stage === "building" || stage === "done") && (
        <ProgressBar progress={progress} building={stage === "building"} done={stage === "done"} />
      )}

      <div className="log" aria-live="polite">
        {messages.map((m, i) => (
          <InView
            key={m.id}
            className={`msg ${m.role}`}
          >
            {m.role === "note" && stage === "building" && i === messages.length - 1 ? (
              <TextShimmer>{m.text}</TextShimmer>
            ) : (
              m.text
            )}
          </InView>
        ))}

        {options.length > 0 && (
          <div className="chips-wrap">
            <div className="chips" role="group" aria-label={multi ? "Select all that apply" : "Pick one"}>
              {options.map((o) => (
                <button
                  key={o}
                  className={picked.includes(o) ? "on" : ""}
                  aria-pressed={multi ? picked.includes(o) : undefined}
                  disabled={busy}
                  onClick={(e) => choose(o, e.detail > 0)}
                >
                  {multi && picked.includes(o) ? "\u2713 " : ""}
                  {o}
                </button>
              ))}
            </div>
            {multi && <span className="hint">Select all that apply, then press Send. You can also type your own below.</span>}
          </div>
        )}

        {busy && stage !== "building" && <div className="msg note">
            <TextShimmer>Thinking...</TextShimmer>
          </div>}

        {stage === "fallback" && (
          <FallbackForm fields={form} disabled={busy} missing={missing} onSubmit={submitForm} />
        )}

        {icp && stage === "done" && (
          <IcpView
            icp={icp}
            approved={approved}
            busy={busy}
            onApprove={approve}
            onImprove={improve}
            onNewChat={newChat}
          />
        )}
        {icp && stage !== "done" && (
          <details className="previous">
            <summary>Show the current profile (being updated)</summary>
            <IcpView icp={icp} approved={approved} busy readOnly onApprove={approve} onImprove={improve} onNewChat={newChat} />
          </details>
        )}
      </div>

      {stage === "interview" && <Composer disabled={busy} sessionId={sessionId} hasPicks={picked.length > 0} onSend={send} />}
    </main>
  );
}
