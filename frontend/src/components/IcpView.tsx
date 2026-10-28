import type { IcpResponse } from "../types";
import { actions, card, primaryBtn } from "../ui";

const H4 = "mt-4 mx-0 mb-1.5 text-[12px] uppercase tracking-[.05em] text-muted";
const label = (s: string) => s.replace(/_/g, " ");

interface Props {
  icp: IcpResponse;
  approved: boolean;
  busy: boolean;
  readOnly?: boolean; // shown for reference while they ask for changes
  onApprove: () => void;
  onImprove: () => void;
  onNewChat: () => void;
}

export default function IcpView({ icp, approved, busy, readOnly, onApprove, onImprove, onNewChat }: Props) {
  const doc = icp.document;

  return (
    <article className={card}>
      <h3 className="mt-0 mx-0 mb-2 text-[15px]">Your Ideal Customer Profile</h3>
      {doc.partial && (
        <p className="text-muted text-[12px]">
          This profile is incomplete: the chat ended before the basics were covered. "Still unclear" lists what is
          missing. Use Needs improvement to add it.
        </p>
      )}
      {doc.summary && <p>{doc.summary}</p>}

      {Object.entries(doc.sections).map(([section, fields]) => (
        <section key={section}>
          <h4 className={H4}>{label(section)}</h4>
          {Object.entries(fields).map(([key, f]) => (
            <div className="flex justify-between gap-3 py-1 border-b border-line last-of-type:border-b-0 max-[520px]:flex-col max-[520px]:gap-0" key={key}>
              <span className="text-muted flex-[0_0_38%] capitalize">{label(key)}</span>
              <span className="flex-1 text-right [overflow-wrap:anywhere] max-[520px]:text-left">
                {f.values.join(", ")}
                {f.source === "inferred" && (
                  <span className="text-[11px] text-muted ml-1.5" title="Guessed from your website, not confirmed">
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
          <h4 className={H4}>Still unclear</h4>
          <ul>
            {doc.open_questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </>
      )}

      {readOnly ? null : approved ? (
        <p className="text-[#4ade80]">Thanks. You confirmed this profile is correct.</p>
      ) : (
        <>
          <h4 className={H4}>Is this profile fine, or does it need improvement?</h4>
          <div className={actions}>
            <button className={primaryBtn} disabled={busy} onClick={onApprove}>
              {"✓"} Looks good
            </button>
            <button disabled={busy} onClick={onImprove}>
              Needs improvement
            </button>
          </div>
        </>
      )}
      {!readOnly && (
        <div className={actions}>
          <button onClick={onNewChat}>New chat</button>
        </div>
      )}
    </article>
  );
}
