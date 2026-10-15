import { useState } from "react";
import type { Disqualifier, IcpEdit, IcpResponse } from "../types";

const label = (s: string) => s.replace(/_/g, " ");
const pct = (n: number) => Math.round(n * 100);
const OPERATORS: Record<Disqualifier["operator"], string> = {
  in: "is one of",
  not_in: "is not one of",
  lt: "is below",
  gt: "is above",
  eq: "is",
};

interface Props {
  icp: IcpResponse;
  approved: boolean;
  busy: boolean;
  // Each returns an error message to show, or null on success.
  onEdit: (edit: IcpEdit) => Promise<string | null>;
  onApprove: () => Promise<string | null>;
  onVerdict: (correct: boolean, comment: string) => Promise<string | null>;
  onNewChat: () => void;
}

export default function IcpView({
  icp,
  approved,
  busy,
  onEdit,
  onApprove,
  onVerdict,
  onNewChat,
}: Props) {
  const doc = icp.document;
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [weights, setWeights] = useState<Record<string, string> | null>(null);
  const [wrong, setWrong] = useState(false);
  const [comment, setComment] = useState("");
  const [sent, setSent] = useState<"correct" | "wrong" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const meters: [string, number][] = [
    ["Core fields", doc.completeness.tier_a],
    ["Extra detail", doc.completeness.tier_b],
  ];
  const locked = busy || approved;

  async function run(job: () => Promise<string | null>, done?: () => void) {
    setError(null);
    const message = await job();
    if (message) setError(message);
    else done?.();
  }

  function startEdit(key: string, values: string[]) {
    setEditing(key);
    setDraft(values.join(", "));
  }

  const saveField = (slot: string, values: string[]) =>
    run(
      () => onEdit({ kind: "field", slot, values }),
      () => setEditing(null),
    );

  // Weights always add up to 100 over all criteria; ones with no data yet are skipped when scoring.
  const criteria = doc.scoring_model.criteria;
  const total = weights ? criteria.reduce((sum, c) => sum + (Number(weights[c.key]) || 0), 0) : 0;

  function saveWeights() {
    if (!weights) return;
    const body = Object.fromEntries(criteria.map((c) => [c.key, Number(weights[c.key]) || 0]));
    return run(
      () => onEdit({ kind: "weights", weights: body }),
      () => setWeights(null),
    );
  }

  const removeRule = (i: number) =>
    run(() =>
      onEdit({
        kind: "disqualifiers",
        disqualifiers: doc.scoring_model.disqualifiers.filter((_, n) => n !== i),
      }),
    );

  async function approveIt() {
    await run(
      async () => (await onApprove()) ?? (await onVerdict(true, "")),
      () => setSent("correct"),
    );
  }

  const reportWrong = () =>
    run(
      () => onVerdict(false, comment),
      () => {
        setSent("wrong");
        setWrong(false);
      },
    );

  return (
    <article className="card icp">
      <h3>Your Ideal Customer Profile</h3>
      {doc.summary && <p>{doc.summary}</p>}
      <p className="hint">
        Check each line. Use Edit to fix anything that is wrong, then tell us below whether the
        profile is correct.
      </p>

      <h4>Completeness</h4>
      {meters.map(([name, v]) => (
        <div key={name} className="meter">
          <span>
            {name}: {pct(v)}%
          </span>
          <div className="bar">
            <i style={{ width: `${pct(v)}%` }} />
          </div>
        </div>
      ))}

      {Object.entries(doc.sections).map(([section, fields]) => (
        <section key={section}>
          <h4>{label(section)}</h4>
          {Object.entries(fields).map(([key, f]) => (
            <div className="row" key={key}>
              <span className="k">{label(key)}</span>
              {editing === key ? (
                <span className="v edit">
                  <input
                    autoFocus
                    value={draft}
                    aria-label={`Edit ${label(key)}`}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter")
                        saveField(
                          key,
                          draft
                            .split(",")
                            .map((x) => x.trim())
                            .filter(Boolean),
                        );
                      if (e.key === "Escape") setEditing(null);
                    }}
                  />
                  <small>Separate several values with commas.</small>
                  <span className="mini">
                    <button
                      className="primary"
                      disabled={busy}
                      onClick={() =>
                        saveField(
                          key,
                          draft
                            .split(",")
                            .map((x) => x.trim())
                            .filter(Boolean),
                        )
                      }
                    >
                      Save
                    </button>
                    <button disabled={busy} onClick={() => saveField(key, [])}>
                      Remove
                    </button>
                    <button onClick={() => setEditing(null)}>Cancel</button>
                  </span>
                </span>
              ) : (
                <span className="v">
                  {f.values.join(", ")}
                  {f.source !== "stated" && <span className="tag">{f.source}</span>}
                  {!locked && (
                    <button className="link" onClick={() => startEdit(key, f.values)}>
                      Edit
                    </button>
                  )}
                </span>
              )}
            </div>
          ))}
        </section>
      ))}

      <h4>
        Scoring model (weights)
        {!locked && !weights && (
          <button
            className="link"
            onClick={() =>
              setWeights(Object.fromEntries(criteria.map((c) => [c.key, String(c.weight)])))
            }
          >
            Edit weights
          </button>
        )}
      </h4>
      {criteria.map((c) => (
        <div className="row" key={c.key}>
          <span className="k">{c.label}</span>
          <span className="v">
            {weights ? (
              <input
                className="num"
                type="number"
                min={0}
                max={100}
                value={weights[c.key] ?? ""}
                aria-label={`Weight for ${c.label}`}
                onChange={(e) => setWeights({ ...weights, [c.key]: e.target.value })}
              />
            ) : (
              c.weight
            )}
            {!c.applicable && <span className="tag">no data yet, not scored</span>}
            {c.applicable && !weights && (
              <button
                className="link"
                disabled={locked}
                title="Required means a lead without it cannot match"
                onClick={() =>
                  run(() =>
                    onEdit({
                      kind: "requirement",
                      criterion: c.key,
                      requirement: c.requirement === "required" ? "preferred" : "required",
                    }),
                  )
                }
              >
                {c.requirement}
              </button>
            )}
          </span>
        </div>
      ))}
      {weights && (
        <div className="actions">
          <span className={total === 100 ? "ok" : "bad"}>Total: {total} (must be 100)</span>
          <button className="primary" disabled={busy || total !== 100} onClick={saveWeights}>
            Save weights
          </button>
          <button onClick={() => setWeights(null)}>Cancel</button>
        </div>
      )}

      {doc.scoring_model.disqualifiers.length > 0 && (
        <>
          <h4>Leads are excluded when</h4>
          {doc.scoring_model.disqualifiers.map((d, i) => (
            <div className="row" key={`${d.field}-${i}`}>
              <span className="k">{label(d.field)}</span>
              <span className="v">
                {OPERATORS[d.operator]} {d.values.join(", ")}
                {!locked && (
                  <button className="link" onClick={() => removeRule(i)}>
                    Remove
                  </button>
                )}
              </span>
            </div>
          ))}
        </>
      )}

      {doc.open_questions.length > 0 && (
        <>
          <h4>Still unclear</h4>
          <ul>
            {doc.open_questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </>
      )}

      {error && (
        <p className="msg error" role="alert">
          {error}
        </p>
      )}

      <h4>Is this profile correct?</h4>
      {sent === "correct" || approved ? (
        <p className="ok">Thanks. You confirmed this profile is correct.</p>
      ) : sent === "wrong" ? (
        <p className="ok">
          Thanks, we recorded that it is not right. Fix the lines above, then confirm it.
        </p>
      ) : null}
      {!approved && (
        <div className="actions">
          <button className="primary" disabled={busy || weights !== null} onClick={approveIt}>
            {"✓"} Yes, this is correct
          </button>
          <button disabled={busy} onClick={() => setWrong((w) => !w)}>
            {"✗"} Not quite right
          </button>
        </div>
      )}
      {wrong && !approved && (
        <div className="feedback">
          <textarea
            rows={3}
            maxLength={2000}
            value={comment}
            placeholder="What is wrong or missing? (optional)"
            onChange={(e) => setComment(e.target.value)}
          />
          <button className="primary" disabled={busy} onClick={reportWrong}>
            Send feedback
          </button>
        </div>
      )}
      <div className="actions">
        <button onClick={onNewChat}>New chat</button>
      </div>
    </article>
  );
}
