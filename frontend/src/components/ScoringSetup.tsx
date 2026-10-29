import { useEffect, useState } from "react";
import * as api from "../api";
import type { IcpResponse, PriorityAnswer, PriorityQuestion } from "../types";
import { actions, primaryBtn } from "../ui";

const H4 = "mt-4 mx-0 mb-1.5 text-[12px] uppercase tracking-[.05em] text-muted";

interface Props {
  sessionId: string;
  icp: IcpResponse;
  busy: boolean;
  onIcp: (icp: IcpResponse) => void;
}

// "Which matters more?" questions turn into the weights this ICP scores leads with; the table that
// follows lets them adjust each weight and mark a criterion as a must-have.
export default function ScoringSetup({ sessionId, icp, busy, onIcp }: Props) {
  const [questions, setQuestions] = useState<PriorityQuestion[] | null>(null);
  const [step, setStep] = useState(0);
  const [picked, setPicked] = useState<Record<number, number>>({});
  const [phase, setPhase] = useState<"ask" | "table">("ask");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  const criteria = icp.document.scoring_model.criteria.filter((c) => c.applicable);
  const weightsKey = criteria.map((c) => `${c.key}:${c.weight}`).join("|");

  useEffect(() => {
    api
      .getScoringQuestions(sessionId)
      .then((qs) => {
        setQuestions(qs);
        if (qs.length === 0) setPhase("table");
      })
      .catch(() => setPhase("table"));
  }, [sessionId]);

  useEffect(() => {
    setDraft(Object.fromEntries(criteria.map((c) => [c.key, String(c.weight)])));
  }, [weightsKey]);

  async function run(edit: Parameters<typeof api.editIcp>[1]) {
    setWorking(true);
    setError("");
    try {
      onIcp(await api.editIcp(sessionId, edit));
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save");
      return false;
    } finally {
      setWorking(false);
    }
  }

  async function finish() {
    if (!questions) return;
    const answers: PriorityAnswer[] = questions.map((q, i) => ({ a: q.a, b: q.b, value: picked[i] ?? 0 }));
    if (await run({ kind: "priorities", answers })) setPhase("table");
  }

  const disabled = busy || working;
  const total = criteria.reduce((sum, c) => sum + (parseFloat(draft[c.key]) || 0), 0);
  const sums = Math.abs(total - 100) < 1e-6;
  const dirty = criteria.some((c) => (parseFloat(draft[c.key]) || 0) !== c.weight);

  if (phase === "ask" && questions && questions.length > 0) {
    const q = questions[step];
    const options: [number, string][] = [
      [2, `${q.a_label} much more`],
      [1, `${q.a_label} somewhat`],
      [0, "About equal"],
      [-1, `${q.b_label} somewhat`],
      [-2, `${q.b_label} much more`],
    ];
    return (
      <section aria-label="Scoring priorities">
        <h4 className={H4}>How should leads be scored?</h4>
        <p className="text-muted text-[12px] m-0">
          Question {step + 1} of {questions.length}
        </p>
        <p className="mt-1 mb-2">
          <b>
            Which matters more: {q.a_label} or {q.b_label}?
          </b>
        </p>
        <div className="grid grid-cols-2 gap-2 text-[13px] mb-2">
          <div>
            <b>{q.a_label}</b>
            <div className="text-muted">{q.a_looks}</div>
          </div>
          <div>
            <b>{q.b_label}</b>
            <div className="text-muted">{q.b_looks}</div>
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          {options.map(([value, text]) => (
            <label key={value} className="flex items-center gap-2">
              <input type="radio" name="priority" checked={picked[step] === value} onChange={() => setPicked({ ...picked, [step]: value })} />
              <span>{text}</span>
            </label>
          ))}
        </div>
        {error && <p className="text-warn text-[12px]">{error}</p>}
        <div className={actions}>
          <button disabled={disabled || step === 0} onClick={() => setStep(step - 1)}>
            Back
          </button>
          {step < questions.length - 1 ? (
            <button className={primaryBtn} disabled={disabled || picked[step] === undefined} onClick={() => setStep(step + 1)}>
              Next
            </button>
          ) : (
            <button className={primaryBtn} disabled={disabled || picked[step] === undefined} onClick={finish}>
              Set my weights
            </button>
          )}
          <button disabled={disabled} onClick={() => setPhase("table")}>
            Skip
          </button>
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Scoring weights">
      <h4 className={H4}>Scoring weights</h4>
      <p className="text-muted text-[12px] mt-0">How much each part of a lead counts toward its match score. Must-have means a lead without it is not qualified.</p>
      {criteria.map((c) => (
        <div className="flex items-center gap-3 py-1 border-b border-line last-of-type:border-b-0" key={c.key}>
          <span className="flex-1">{c.label}</span>
          <input
            className="w-[64px] text-right"
            inputMode="decimal"
            aria-label={`${c.label} weight`}
            value={draft[c.key] ?? ""}
            disabled={disabled}
            onChange={(e) => setDraft({ ...draft, [c.key]: e.target.value })}
          />
          <label className="flex items-center gap-1.5 text-[12px] text-muted">
            <input
              type="checkbox"
              checked={c.requirement === "required"}
              disabled={disabled}
              onChange={(e) => void run({ kind: "requirement", criterion: c.key, requirement: e.target.checked ? "required" : "preferred" })}
            />
            Must-have
          </label>
        </div>
      ))}
      <p className={`text-[12px] ${sums ? "text-muted" : "text-warn"}`}>
        Total {Math.round(total * 10) / 10}
        {sums ? "" : " (weights must add up to 100)"}
      </p>
      {error && <p className="text-warn text-[12px]">{error}</p>}
      <div className={actions}>
        <button
          className={primaryBtn}
          disabled={disabled || !dirty || !sums}
          onClick={() => void run({ kind: "weights", weights: { ...Object.fromEntries(icp.document.scoring_model.criteria.map((c) => [c.key, c.weight])), ...Object.fromEntries(criteria.map((c) => [c.key, parseFloat(draft[c.key]) || 0])) } })}
        >
          Save weights
        </button>
        {questions && questions.length > 0 && (
          <button disabled={disabled} onClick={() => { setStep(0); setPicked({}); setPhase("ask"); }}>
            Redo the questions
          </button>
        )}
      </div>
    </section>
  );
}
