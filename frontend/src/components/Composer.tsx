import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import { canRecord, MAX_RECORD_SECONDS, startRecording, type Recording } from "../audio";

const MAX_CHARS = 2000;
const AUTO_SEND_DELAY_MS = 2000; // time to cancel a wrong transcript
const AUTO_SEND_KEY = "icp.voice.autoSend";

const Icon = ({ d }: { d: string }) => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={d} />
  </svg>
);
const MIC = "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM19 11a7 7 0 0 1-14 0M12 18v3";
const STOP = "M7 7h10v10H7z";
const WAIT = "M12 6v6l3 2";
const SEND = "M12 19V5M5 12l7-7 7 7";

interface Props {
  disabled: boolean;
  sessionId: string | null;
  hasPicks?: boolean; // chips are selected, so an empty text box can still send
  onSend: (text: string) => void;
}

type Phase = "idle" | "recording" | "transcribing";

function voiceError(e: unknown): string {
  if (e instanceof DOMException && (e.name === "NotAllowedError" || e.name === "SecurityError")) {
    return "Microphone blocked. Allow it in your browser to use voice.";
  }
  if (e instanceof DOMException && e.name === "NotFoundError") return "No microphone found.";
  if (e instanceof api.ApiError && e.status === 429) return "Too many requests. Wait a moment and try again.";
  return "Voice input failed. Please try again or type your answer.";
}

export default function Composer({ disabled, sessionId, hasPicks = false, onSend }: Props) {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [voiceMsg, setVoiceMsg] = useState("");
  const [pendingSend, setPendingSend] = useState(false);
  const [autoSend, setAutoSend] = useState(() => {
    try {
      return localStorage.getItem(AUTO_SEND_KEY) === "1";
    } catch {
      return false;
    }
  });
  const ref = useRef<HTMLTextAreaElement>(null);
  const recording = useRef<Recording | null>(null);
  const limitTimer = useRef<number>();

  useEffect(() => {
    if (!disabled) ref.current?.focus();
  }, [disabled]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [text]);

  // leaving the page or the interview releases the microphone
  useEffect(
    () => () => {
      window.clearTimeout(limitTimer.current);
      recording.current?.cancel();
    },
    [],
  );

  // auto-send: a short pause after the transcript lands, so a wrong one can be cancelled or edited
  useEffect(() => {
    if (!pendingSend || disabled) return;
    const t = window.setTimeout(send, AUTO_SEND_DELAY_MS);
    return () => window.clearTimeout(t);
  });

  function send() {
    const t = text.trim();
    setPendingSend(false);
    if ((!t && !hasPicks) || disabled) return;
    setText("");
    onSend(t);
  }

  function changeAutoSend(on: boolean) {
    setAutoSend(on);
    if (!on) setPendingSend(false);
    try {
      localStorage.setItem(AUTO_SEND_KEY, on ? "1" : "0");
    } catch {
      // the choice just is not remembered
    }
  }

  async function finishRecording() {
    const rec = recording.current;
    if (!rec || !sessionId) return;
    recording.current = null;
    window.clearTimeout(limitTimer.current);
    setPhase("transcribing");
    try {
      const spoken = (await api.transcribe(sessionId, await rec.stop())).trim();
      if (!spoken) {
        setVoiceMsg("I didn't catch that. Try again or type your answer.");
      } else {
        setText((prev) => (prev.trim() ? `${prev.trimEnd()} ${spoken}` : spoken).slice(0, MAX_CHARS));
        setPendingSend(autoSend);
      }
    } catch (e) {
      setVoiceMsg(voiceError(e));
    } finally {
      setPhase("idle");
    }
  }

  async function toggleMic() {
    if (phase === "recording") return void finishRecording();
    if (phase !== "idle" || !sessionId) return;
    setVoiceMsg("");
    setPendingSend(false);
    try {
      recording.current = await startRecording();
    } catch (e) {
      setVoiceMsg(voiceError(e));
      return;
    }
    setPhase("recording");
    limitTimer.current = window.setTimeout(() => void finishRecording(), MAX_RECORD_SECONDS * 1000);
  }

  const micLabel =
    phase === "recording" ? "Stop recording" : phase === "transcribing" ? "Transcribing" : "Speak your answer";
  const status =
    phase === "recording"
      ? "Listening... tap the square to stop."
      : phase === "transcribing"
        ? "Transcribing..."
        : pendingSend
          ? "Sending in a moment..."
          : voiceMsg;

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <div className="prompt-box">
        <textarea
          ref={ref}
          rows={1}
          maxLength={MAX_CHARS}
          placeholder={hasPicks ? "Add anything else (optional)" : "Type your answer"}
          disabled={disabled}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setPendingSend(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="prompt-actions">
          {canRecord() && sessionId && (
            <button
              type="button"
              className={`mic${phase === "recording" ? " on" : ""}`}
              disabled={disabled || phase === "transcribing"}
              onClick={() => void toggleMic()}
              aria-pressed={phase === "recording"}
              aria-label={micLabel}
              title={micLabel}
            >
              <Icon d={phase === "recording" ? STOP : phase === "transcribing" ? WAIT : MIC} />
            </button>
          )}
          <button
            className="send"
            type="submit"
            aria-label="Send"
            title="Send"
            disabled={disabled || phase !== "idle" || (!text.trim() && !hasPicks)}
          >
            <Icon d={SEND} />
          </button>
        </div>
      </div>
      {(canRecord() && sessionId) || status ? (
        <div className="voice-row" role="status">
          <span className={voiceMsg && phase === "idle" && !pendingSend ? "mic-error" : ""}>{status}</span>
          {pendingSend && (
            <button type="button" className="link" onClick={() => setPendingSend(false)}>
              Cancel
            </button>
          )}
          {canRecord() && sessionId && (
            <label className="auto-send">
              <input type="checkbox" checked={autoSend} onChange={(e) => changeAutoSend(e.target.checked)} />
              Auto-send voice answers
            </label>
          )}
        </div>
      ) : null}
    </form>
  );
}
