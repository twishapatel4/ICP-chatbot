import { useEffect, useState } from "react";
import * as api from "../api";
import type {
  Criterion,
  IcpResponse,
  PriorityAnswer,
  PriorityQuestion,
} from "../types";
import { actions, card, pillInput, primaryBtn } from "../ui";

const CARD = `${card} flex flex-col gap-4`;
const TITLE = "m-0 text-[16px] font-medium tracking-[-.01em]";
const HINT = "m-0 text-[13px] leading-[1.5] text-muted";
// a pick shown as a bordered pill: the radio or checkbox inside says what kind of pick it is
const PILL =
  "flex items-center gap-2.5 rounded-[18px] border bg-panel py-2.5 px-4 text-[14px] cursor-pointer";

// What the ICP looks for on a criterion, in the company's own values.
export function looksFor(c: Criterion): string {
  const parts: string[] = [];
  for (const rule of Object.values(c.rules)) {
    if (rule.kind === "open") parts.push("Any");
    else if (rule.kind === "list") parts.push(...rule.primary);
    else if (rule.min !== null && rule.max !== null)
      parts.push(`${rule.min}-${rule.max}`);
    else if (rule.min !== null) parts.push(`${rule.min}+`);
    else if (rule.max !== null) parts.push(`up to ${rule.max}`);
  }
  return parts.join(", ");
}

const key = (q: PriorityQuestion) => `${q.a}|${q.b}`;
const round1 = (n: number) => Math.round(n * 10) / 10;

// What they answered and whether they got past the questions; the parent keeps it so leaving the
// screen and coming back finds the table and the answers as they were.
export interface ScoringProgress {
  done: boolean;
  picked: Record<string, number>; // "a|b" -> answer
}

interface Props {
  progress: ScoringProgress;
  onProgress: (p: ScoringProgress) => void;
  sessionId: string;
  icp: IcpResponse;
  busy: boolean;
  onIcp: (icp: IcpResponse) => void;
  onBack: () => void; // back to the profile
  onDone: () => void;
}

// 1. "Which matters more?" questions, built from the criteria this ICP has values for.
// 2. A table of every weight: edit weights, mark must-haves and set the threshold, then save.
export default function ScoringSetup({
  progress,
  onProgress,
  sessionId,
  icp,
  busy,
  onIcp,
  onBack,
  onDone,
}: Props) {
  const model = icp.document.scoring_model;
  const criteria = model.criteria.filter((c) => c.applicable);
  const weightsKey =
    criteria.map((c) => `${c.key}:${c.weight}:${c.requirement}`).join("|") +
    `|${model.threshold}`;

  const [questions, setQuestions] = useState<PriorityQuestion[] | null>(null);
  const [step, setStep] = useState(0);
  const picked = progress.picked;
  const [phase, setPhase] = useState<"ask" | "table">(progress.done ? "table" : "ask");
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [must, setMust] = useState<Record<string, boolean>>({});
  const [threshold, setThreshold] = useState(model.threshold);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .getScoringQuestions(sessionId)
      .then((qs) => {
        setQuestions(qs);
        if (qs.length === 0) setPhase("table");
      })
      .catch(() => setPhase("table"));
  }, [sessionId]);

  // once the table has been shown, coming back later opens it: the questions are not asked again
  useEffect(() => {
    if (phase === "table" && !progress.done) onProgress({ ...progress, done: true });
  }, [phase, progress.done]);

  // the table starts from what the document holds, and again whenever the answers change it
  useEffect(() => {
    setWeights(
      Object.fromEntries(criteria.map((c) => [c.key, String(c.weight)])),
    );
    setMust(
      Object.fromEntries(
        criteria.map((c) => [c.key, c.requirement === "required"]),
      ),
    );
    setThreshold(model.threshold);
  }, [weightsKey]);

  const disabled = busy || working;
  const w = (k: string) => parseFloat(weights[k]) || 0;
  const total = criteria.reduce((sum, c) => sum + w(c.key), 0);
  const sums = Math.abs(total - 100) < 1e-6;
  const dirty =
    threshold !== model.threshold ||
    criteria.some(
      (c) =>
        w(c.key) !== c.weight || must[c.key] !== (c.requirement === "required"),
    );

  async function edits(list: Parameters<typeof api.editIcp>[1][]) {
    setWorking(true);
    setError("");
    try {
      let latest: IcpResponse | null = null;
      for (const edit of list) latest = await api.editIcp(sessionId, edit);
      if (latest) onIcp(latest);
      return true;
    } catch (e) {
      setError(
        e instanceof api.ApiError && e.status === 404
          ? "We could not find this profile on the server any more. Start a new chat to build it again."
          : "Could not save. Please try again.",
      );
      return false;
    } finally {
      setWorking(false);
    }
  }

  async function finishQuestions() {
    if (!questions) return;
    const answers: PriorityAnswer[] = questions.map((q) => ({
      a: q.a,
      b: q.b,
      value: picked[key(q)] ?? 0,
    }));
    if (await edits([{ kind: "priorities", answers }])) {
      onProgress({ ...progress, done: true });
      setPhase("table");
    }
  }

  async function save(): Promise<boolean> {
    const all = Object.fromEntries(
      model.criteria.map((c) => [c.key, c.weight]),
    );
    const list: Parameters<typeof api.editIcp>[1][] = [
      {
        kind: "weights",
        weights: {
          ...all,
          ...Object.fromEntries(criteria.map((c) => [c.key, w(c.key)])),
        },
      },
    ];
    for (const c of criteria)
      if (must[c.key] !== (c.requirement === "required"))
        list.push({
          kind: "requirement",
          criterion: c.key,
          requirement: must[c.key] ? "required" : "preferred",
        });
    if (threshold !== model.threshold)
      list.push({ kind: "threshold", threshold });
    if (await edits(list)) {
      onProgress({ ...progress, done: true });
      return true;
    }
    return false;
  }

  // Done saves any change first, and stays here if that fails
  async function done() {
    if (phase === "table" && dirty && !(await save())) return;
    onDone();
  }

  if (phase === "ask" && questions && questions.length > 0) {
    const q = questions[step];
    const options: [number, string][] = [
      [2, `${q.a_label} much more`],
      [1, `${q.a_label} somewhat`],
      [0, "About equal"],
      [-1, `${q.b_label} somewhat`],
      [-2, `${q.b_label} much more`],
    ];
    const last = step === questions.length - 1;
    return (
      <div className="flex flex-col gap-4">
        <p className="m-0 -mt-3 text-center text-[14px] text-muted max-w-[520px] mx-auto">
          Tell us what matters more when matching leads to this profile. The answers set the weights, and you can adjust them after.
        </p>
        <section className={CARD} aria-label="Scoring priorities">
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between text-[12px] text-muted">
              <span>How should leads be scored?</span>
              <span>
                Question {step + 1} of {questions.length}
              </span>
            </div>
            <div
              className="h-1 rounded-full bg-panel shadow-[inset_0_0_0_1px_var(--line)] overflow-hidden"
              aria-hidden="true"
            >
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-300"
                style={{ width: `${((step + 1) / questions.length) * 100}%` }}
              />
            </div>
          </div>
          <h2 className="m-0 text-[clamp(18px,2.4vw,22px)] leading-[1.25] font-medium tracking-[-.02em]">
            Which matters more: {q.a_label} or {q.b_label}?
          </h2>
          <div className="grid grid-cols-2 gap-3 max-[560px]:grid-cols-1">
            {[
              [q.a_label, q.a_looks],
              [q.b_label, q.b_looks],
            ].map(([label, looks]) => (
              <div
                key={label}
                className="rounded-[18px] border border-line bg-panel p-3.5"
              >
                <div className="text-[14px] font-medium">{label}</div>
                <div className="mt-0.5 text-[13px] text-muted">{looks}</div>
              </div>
            ))}
          </div>
          <div
            className="flex flex-col gap-2"
            role="radiogroup"
            aria-label="Which matters more"
          >
            {options.map(([value, text]) => (
              <label
                key={value}
                className={`${PILL} ${picked[key(q)] === value ? "border-accent" : "border-line"}`}
              >
                <input
                  className="size-4 accent-accent m-0"
                  type="radio"
                  name="priority"
                  checked={picked[key(q)] === value}
                  onChange={() =>
                    onProgress({
                      ...progress,
                      picked: { ...picked, [key(q)]: value },
                    })
                  }
                />
                {text}
              </label>
            ))}
          </div>
          {error && <p className="m-0 text-warn text-[13px]">{error}</p>}
          <div className={`${actions} mt-0`}>
            <button
              disabled={disabled || step === 0}
              onClick={() => setStep(step - 1)}
            >
              Back
            </button>
            <button
              className={primaryBtn}
              disabled={disabled || picked[key(q)] === undefined}
              onClick={() =>
                last ? void finishQuestions() : setStep(step + 1)
              }
            >
              {last ? "Build my scoring table" : "Next"}
            </button>
          </div>
        </section>
        <div className="flex justify-center">
          <button disabled={disabled} onClick={onBack}>
            Back to profile
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4" aria-label="Scoring weights">
      <section className={CARD}>
        <p className={HINT}>
          Built from your answers, and you can change it. A must-have means a
          lead that does not match it is left out, whatever its score.
        </p>
        <ul className="list-none m-0 p-0 flex flex-col divide-y divide-line border-y border-line">
          {criteria.map((c) => (
            <li
              key={c.key}
              className="grid grid-cols-[1fr_auto_auto] items-center gap-x-4 gap-y-2 py-3 max-[640px]:grid-cols-[1fr_auto]"
            >
              <div className="min-w-0 max-[640px]:col-span-2">
                <div className="text-[14px] font-medium">{c.label}</div>
                <div className="text-[13px] text-muted [overflow-wrap:anywhere]">
                  {looksFor(c)}
                </div>
              </div>
              <label className="flex items-center gap-2 text-[12px] text-muted">
                Weight
                <input
                  className={`${pillInput} w-[76px] py-1.5 px-3 text-center text-ink`}
                  inputMode="decimal"
                  aria-label={`${c.label} weight`}
                  value={weights[c.key] ?? ""}
                  disabled={disabled}
                  onChange={(e) =>
                    setWeights({ ...weights, [c.key]: e.target.value })
                  }
                />
              </label>
              <label
                className={`${PILL} py-1.5 px-3.5 text-[13px] ${must[c.key] ? "border-accent" : "border-line"}`}
              >
                <input
                  className="size-3.5 accent-accent m-0"
                  type="checkbox"
                  checked={!!must[c.key]}
                  disabled={disabled}
                  onChange={(e) =>
                    setMust({ ...must, [c.key]: e.target.checked })
                  }
                />
                Must-have
              </label>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap items-center justify-end gap-3 text-[13px]">
          {!sums && (
            <span className="text-warn">Weights must add up to 100</span>
          )}
          <span className="text-muted">Total</span>
          <span
            className={`rounded-full border bg-panel py-1 px-3 font-medium tabular-nums ${sums ? "border-line" : "border-warn text-warn"}`}
          >
            {round1(total)} / 100
          </span>
        </div>
      </section>

      <section className={CARD}>
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h2 className={TITLE}>Threshold for this campaign</h2>
            <p className={HINT}>
              Leads scoring <b className="font-medium text-ink">{threshold}</b>{" "}
              or more go into the sequence.
            </p>
          </div>
          <span className="rounded-full border border-line bg-panel py-1 px-4 text-[22px] font-medium tabular-nums">
            {threshold}
          </span>
        </div>
        <div>
          <input
            type="range"
            className="w-full accent-accent"
            min={0}
            max={100}
            step={5}
            value={threshold}
            disabled={disabled}
            aria-label="Score threshold"
            onChange={(e) => setThreshold(parseInt(e.target.value, 10))}
          />
          <div
            className="relative mt-1 h-4 text-[12px] text-muted"
            aria-hidden="true"
          >
            <span className="absolute left-0">0 (everyone)</span>
            <span className="absolute left-1/2 -translate-x-1/2">50</span>
            <span className="absolute right-0">100 (perfect match only)</span>
          </div>
        </div>
      </section>

      {error && (
        <p className="m-0 text-center text-warn text-[13px]">{error}</p>
      )}
      <div className={`${actions} mt-0 justify-center`}>
        {questions && questions.length > 0 && (
          <button
            disabled={disabled}
            onClick={() => {
              setStep(0);
              setPhase("ask");
            }}
          >
            Edit my answers
          </button>
        )}
        <button disabled={disabled} onClick={onBack}>
          Back to profile
        </button>
        <button
          className={primaryBtn}
          disabled={disabled || !sums}
          onClick={() => void done()}
        >
          Done
        </button>
      </div>
    </div>
  );
}
