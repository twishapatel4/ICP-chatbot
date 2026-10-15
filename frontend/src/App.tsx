import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import Composer from "./components/Composer";
import FallbackForm from "./components/FallbackForm";
import IcpView from "./components/IcpView";
import SignupCard from "./components/SignupCard";
import type { FormField, IcpResponse, StartRequest, Turn } from "./types";

type Stage = "home" | "signup" | "interview" | "fallback" | "building" | "done";
interface Message {
  id: number;
  role: "bot" | "me" | "note" | "error";
  text: string;
}

function explain(e: unknown): string {
  if (!(e instanceof api.ApiError)) return "Could not reach the server. Is the API running?";
  switch (e.status) {
    case 409:
      return "That answer was already received, or the chat is closed.";
    case 422:
      return `That could not be used: ${e.message}`;
    case 429:
      return "Daily or rate limit reached. Please try again later.";
    default:
      return `Something went wrong (${e.message}).`;
  }
}

export default function App() {
  const [stage, setStage] = useState<Stage>("home");
  const [messages, setMessages] = useState<Message[]>([]);
  const [options, setOptions] = useState<string[]>([]);
  const [multi, setMulti] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [form, setForm] = useState<FormField[]>([]);
  const [missing, setMissing] = useState<string[]>([]);
  const [icp, setIcp] = useState<IcpResponse | null>(null);
  const [approved, setApproved] = useState(false);
  const nextId = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);

  const push = useCallback((role: Message["role"], text: string) => {
    setMessages((m) => [...m, { id: nextId.current++, role, text }]);
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, stage, icp, options]);

  function newChat() {
    setMessages([]);
    setOptions([]);
    setPicked([]);
    setSessionId(null);
    setForm([]);
    setMissing([]);
    setIcp(null);
    setApproved(false);
    setBusy(false);
    setStage("signup");
    push("bot", "Hi! Tell me a little about your company and we will begin.");
  }

  async function makeIcp(id: string) {
    setStage("building");
    push("note", "Building your ICP. This can take a minute...");
    try {
      setIcp(await api.buildIcp(id));
      setStage("done");
    } catch (e) {
      push("error", explain(e));
      setStage("done");
    }
  }

  async function showFallback() {
    setOptions([]);
    push("bot", "I could not continue the conversation, so here is a short form instead.");
    try {
      setForm(await api.getFallbackForm());
      setStage("fallback");
    } catch (e) {
      push("error", explain(e));
    }
  }

  async function handleTurn(turn: Turn) {
    setSessionId(turn.session_id);
    if (turn.status === "fallback" || turn.fallback_form) return showFallback();
    if (turn.question) {
      push("bot", turn.question.text);
      setOptions(turn.status === "active" ? turn.question.options : []);
      setMulti(turn.question.multi);
      setPicked([]);
    }
    if (turn.status === "completed") {
      if (!turn.question) push("bot", "Thanks, that is everything I need.");
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
    return guarded(async () => handleTurn(await api.sendAnswer(sessionId, text)));
  };

  const choose = (o: string) => {
    if (!multi) return send(o);
    setPicked((p) => (p.includes(o) ? p.filter((x) => x !== o) : [...p, o]));
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
      setApproved(true);
    });

  if (stage === "home") {
    return (
      <main className="app home">
        <h1>Build your Ideal Customer Profile</h1>
        <p>
          A short conversation about your business. At the end you get an ICP and a scoring model
          you can review and approve.
        </p>
        <button className="primary big" onClick={newChat}>
          New chat
        </button>
      </main>
    );
  }

  return (
    <main className="app">
      <header>
        <h1>ICP Chatbot</h1>
        <button onClick={newChat} disabled={busy}>
          New chat
        </button>
      </header>

      <div className="log" aria-live="polite">
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.text}
          </div>
        ))}

        {stage === "signup" && <SignupCard disabled={busy} onStart={start} />}

        {options.length > 0 && (
          <div className="chips" role="group" aria-label={multi ? "Select all that apply" : "Pick one"}>
            {options.map((o) => (
              <button
                key={o}
                className={picked.includes(o) ? "on" : ""}
                aria-pressed={multi ? picked.includes(o) : undefined}
                disabled={busy}
                onClick={() => choose(o)}
              >
                {multi && picked.includes(o) ? "\u2713 " : ""}
                {o}
              </button>
            ))}
            {multi && <span className="hint">Select all that apply, or type your own below.</span>}
          </div>
        )}

        {busy && stage !== "building" && <div className="msg note">Thinking...</div>}

        {stage === "fallback" && (
          <FallbackForm fields={form} disabled={busy} missing={missing} onSubmit={submitForm} />
        )}

        {icp && (
          <IcpView icp={icp} approved={approved} busy={busy} onApprove={approve} onNewChat={newChat} />
        )}
        <div ref={endRef} />
      </div>

      {stage === "interview" && <Composer disabled={busy} hasPicks={picked.length > 0} onSend={send} />}
    </main>
  );
}
