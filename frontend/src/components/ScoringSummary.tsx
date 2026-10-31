import type { IcpResponse } from "../types";
import { card } from "../ui";
import { looksFor } from "./ScoringSetup";

// The saved scoring model, for reading: each criterion with what it looks for, its weight and whether
// it is a must-have, then the threshold. Editing happens on the scoring screen.
export default function ScoringSummary({ icp }: { icp: IcpResponse }) {
  const model = icp.document.scoring_model;
  const criteria = model.criteria.filter((c) => c.applicable);

  return (
    <section className={`${card} flex flex-col gap-4`} aria-label="Scoring model">
      <div className="flex flex-col gap-1">
        <h2 className="m-0 text-[16px] font-medium tracking-[-.01em]">Your scoring model</h2>
        <p className="m-0 text-[13px] leading-[1.5] text-muted">
          Leads scoring <b className="font-medium text-ink">{model.threshold}</b> or more go into the sequence.
        </p>
      </div>
      <ul className="list-none m-0 p-0 flex flex-col divide-y divide-line border-y border-line">
        {criteria.map((c) => (
          <li key={c.key} className="flex items-center justify-between gap-4 py-3">
            <div className="min-w-0">
              <div className="text-[14px] font-medium">{c.label}</div>
              <div className="text-[13px] text-muted [overflow-wrap:anywhere]">{looksFor(c) || "Any"}</div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {c.requirement === "required" && (
                <span className="rounded-full border border-accent bg-panel py-0.5 px-2.5 text-[12px]">Must-have</span>
              )}
              <span className="rounded-full border border-line bg-panel py-1 px-3 text-[13px] font-medium tabular-nums">{c.weight}</span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
