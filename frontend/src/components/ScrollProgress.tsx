// Thin bar that fills as the page is scrolled, after motion-primitives' ScrollProgress
// (https://motion-primitives.com/docs/scroll-progress).
import { motion, useScroll, useSpring } from "motion/react";
import type { RefObject } from "react";

interface Props {
  className?: string;
  containerRef?: RefObject<HTMLElement>; // scrolls the page when omitted
}

export default function ScrollProgress({ className, containerRef }: Props) {
  const { scrollYProgress } = useScroll(containerRef ? { container: containerRef } : undefined);
  const scaleX = useSpring(scrollYProgress, { stiffness: 200, damping: 50, restDelta: 0.001 });

  return (
    <motion.div
      className={`fixed inset-x-0 top-0 h-1 z-20 origin-left bg-(image:--grad-progress)${className ? ` ${className}` : ""}`}
      style={{ scaleX }}
      aria-hidden="true"
    />
  );
}
