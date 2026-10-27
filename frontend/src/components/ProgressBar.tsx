import type { Progress } from "../types";

interface Props {
  progress: Progress;
  building: boolean; // the interview is over and the profile is being written
  done: boolean;
}

export default function ProgressBar({ progress, building, done }: Props) {
  const percent = done ? 100 : building ? Math.max(progress.percent, 95) : progress.percent;
  const detail = done
    ? "Profile ready"
    : building
      ? "Writing your profile"
      : progress.total > 0
        ? `${progress.known} of ${progress.total} key details`
        : "";

  return (
    <div
      className={`progress${done ? " done" : ""}`}
      role="progressbar"
      aria-label="ICP progress"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      aria-valuetext={`${percent}%${detail ? `, ${detail}` : ""}`}
    >
      <div className="progress-head">
        <span>ICP progress{detail && <span className="progress-detail"> · {detail}</span>}</span>
        <span className="progress-pct">{percent}%</span>
      </div>
      <div className="progress-track">
        <div className={`progress-fill${building ? " working" : ""}`} style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
