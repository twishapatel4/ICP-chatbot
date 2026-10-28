import { motion, useSpring, useTransform } from "motion/react";
import { useEffect } from "react";
import AnimatedNumber from "./AnimatedNumber";
import type { Progress } from "../types";
import { column, gutter } from "../ui";

interface Props {
  progress: Progress;
  building: boolean; // the interview is over and the profile is being written
  done: boolean;
}

// the gradient spans the whole track (100cqw), so the fill reveals more colour as it grows
const FILL = "relative h-full rounded-full overflow-hidden transition-shadow duration-[400ms] ease-[ease] bg-size-[100cqw_100%] bg-position-[0_0] bg-no-repeat ";
const IDLE = "bg-(image:--grad-progress) shadow-[0_0_14px_rgba(139,155,255,.55)]";
const DONE = "bg-(image:--grad-done) shadow-[0_0_14px_rgba(52,211,153,.5)]";
// a light sweep while the profile is being written
const WORKING =
  " after:content-[''] after:absolute after:inset-0 after:bg-[linear-gradient(110deg,transparent_30%,rgba(255,255,255,.6)_50%,transparent_70%)] after:bg-[length:200%_100%] after:animate-[shimmer_1.8s_linear_infinite] motion-reduce:after:hidden";

export default function ProgressBar({ progress, building, done }: Props) {
  // the fill shows how much of the ICP is gathered; building or finishing never moves it
  const percent = progress.percent;
  // the fill springs to its value, like the scroll-progress bar from motion-primitives
  const spring = useSpring(percent, { stiffness: 200, damping: 50, restDelta: 0.01 });
  const width = useTransform(spring, (v) => `${v}%`);
  useEffect(() => {
    spring.set(percent);
  }, [spring, percent]);

  const detail = done
    ? "Profile ready"
    : building
      ? "Writing your profile"
      : progress.total > 0
        ? `${progress.known} of ${progress.total} key details`
        : "";

  return (
    <div
      className={`${column} ${gutter} sticky top-0 z-5 pt-2.5 pb-4 bg-[linear-gradient(to_bottom,var(--fade)_78%,transparent)]`}
      role="progressbar"
      aria-label="ICP progress"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      aria-valuetext={`${percent}%${detail ? `, ${detail}` : ""}`}
    >
      <div className="flex items-center justify-between gap-3 mb-[9px] text-[12px] font-semibold text-ink">
        <span>ICP progress{detail && <span className="font-normal text-muted"> · {detail}</span>}</span>
        <span className="py-0.5 px-[11px] rounded-full bg-(image:--bot-grad) border border-line text-[12px] tabular-nums shadow-soft">
          <AnimatedNumber value={percent} />%
        </span>
      </div>
      <div className="h-2.5 rounded-full bg-card shadow-[inset_0_0_0_1px_var(--line)] overflow-hidden @container">
        <motion.div className={`${FILL}${done ? DONE : IDLE}${building ? WORKING : ""}`} style={{ width }} />
      </div>
    </div>
  );
}
