import type { IcpResponse } from "../types";

const label = (s: string) => s.replace(/_/g, " ");
const pct = (n: number) => Math.round(n * 100);

interface Props {
  icp: IcpResponse;
  approved: boolean;
  busy: boolean;
  onApprove: () => void;
  onNewChat: () => void;
}

export default function IcpView({ icp, approved, busy, onApprove, onNewChat }: Props) {
  const doc = icp.document;
  const meters: [string, number][] = [
    ["Core fields", doc.completeness.tier_a],
    ["Extra detail", doc.completeness.tier_b],
  ];

  return (
    <article className="card icp">
      <h3>Your Ideal Customer Profile</h3>
      {doc.summary && <p>{doc.summary}</p>}

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
              <span className="v">
                {f.values.join(", ")}
                {f.source !== "stated" && <span className="tag">{f.source}</span>}
              </span>
            </div>
          ))}
        </section>
      ))}

      <h4>Scoring model (weights)</h4>
      {doc.scoring_model.criteria.map((c) => (
        <div className="row" key={c.key}>
          <span className="k">{c.label}</span>
          <span className="v">{c.applicable ? c.weight : "n/a"}</span>
        </div>
      ))}

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

      <div className="actions">
        <button className="primary" disabled={approved || busy} onClick={onApprove}>
          {approved ? "Approved" : "Approve ICP"}
        </button>
        <button onClick={onNewChat}>New chat</button>
      </div>
    </article>
  );
}
