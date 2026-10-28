import { motion, useSpring, useTransform } from "motion/react";
import { useEffect } from "react";
import AnimatedNumber from "./AnimatedNumber";
import type { Progress } from "../types";

interface Props {
  progress: Progress;
  building: boolean; // the interview is over and the profile is being written
  done: boolean;
}

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
        <span className="progress-pct">
          <AnimatedNumber value={percent} />%
        </span>
      </div>
      <div className="progress-track">
        <motion.div className={`progress-fill${building ? " working" : ""}`} style={{ width }} />
      </div>
    </div>
  );
}
