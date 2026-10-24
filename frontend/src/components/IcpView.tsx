import type { IcpResponse } from "../types";

const label = (s: string) => s.replace(/_/g, " ");

interface Props {
  icp: IcpResponse;
  approved: boolean;
  busy: boolean;
  onApprove: () => void;
  onImprove: () => void;
  onNewChat: () => void;
}

export default function IcpView({ icp, approved, busy, onApprove, onImprove, onNewChat }: Props) {
  const doc = icp.document;

  return (
    <article className="card icp">
      <h3>Your Ideal Customer Profile</h3>
      {doc.summary && <p>{doc.summary}</p>}

      {Object.entries(doc.sections).map(([section, fields]) => (
        <section key={section}>
          <h4>{label(section)}</h4>
          {Object.entries(fields).map(([key, f]) => (
            <div className="row" key={key}>
              <span className="k">{label(key)}</span>
              <span className="v">
                {f.values.join(", ")}
                {f.source === "inferred" && (
                  <span className="tag" title="Guessed from your website, not confirmed">
                    from website, unconfirmed
                  </span>
                )}
              </span>
            </div>
          ))}
        </section>
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

      {approved ? (
        <p className="ok">Thanks. You confirmed this profile is correct.</p>
      ) : (
        <>
          <h4>Is this profile fine, or does it need improvement?</h4>
          <div className="actions">
            <button className="primary" disabled={busy} onClick={onApprove}>
              {"✓"} Looks good
            </button>
            <button disabled={busy} onClick={onImprove}>
              Needs improvement
            </button>
          </div>
        </>
      )}
      <div className="actions">
        <button onClick={onNewChat}>New chat</button>
      </div>
    </article>
  );
}
