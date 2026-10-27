// Shimmering status text, after motion-primitives' TextShimmer (https://motion-primitives.com/docs/text-shimmer):
// a light band sweeps across the text. Colours come from --base-color and --base-gradient-color.
import { motion } from "motion/react";
import { type CSSProperties, type ElementType, useMemo } from "react";

interface Props {
  children: string;
  as?: ElementType;
  className?: string;
  duration?: number; // seconds per sweep
  spread?: number; // width of the band per character, in px
}

export default function TextShimmer({ children, as: Tag = "span", className, duration = 2, spread = 2 }: Props) {
  const MotionTag = useMemo(() => motion.create(Tag), [Tag]);
  const band = children.length * spread;

  return (
    <MotionTag
      className={`text-shimmer${className ? ` ${className}` : ""}`}
      style={{ "--spread": `${band}px` } as CSSProperties}
      initial={{ backgroundPosition: "100% center" }}
      animate={{ backgroundPosition: "0% center" }}
      transition={{ repeat: Infinity, duration, ease: "linear" }}
    >
      {children}
    </MotionTag>
  );
}
