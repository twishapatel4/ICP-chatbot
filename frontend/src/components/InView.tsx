// Animates its content in when it scrolls into view, after motion-primitives' InView
// (https://motion-primitives.com/docs/in-view).
import { motion, useInView, type Transition, type UseInViewOptions, type Variant } from "motion/react";
import { type ReactNode, useRef } from "react";

interface Props {
  children: ReactNode;
  className?: string;
  variants?: { hidden: Variant; visible: Variant };
  transition?: Transition;
  viewOptions?: UseInViewOptions;
  once?: boolean; // play the first time only (default)
}

const DEFAULT: NonNullable<Props["variants"]> = {
  hidden: { opacity: 0, y: 18, filter: "blur(6px)" },
  visible: { opacity: 1, y: 0, filter: "blur(0px)" },
};

export default function InView({
  children,
  className,
  variants = DEFAULT,
  transition = { duration: 0.45, ease: "easeOut" },
  viewOptions,
  once = true,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { once, ...viewOptions });

  return (
    <motion.div
      ref={ref}
      className={className}
      initial="hidden"
      animate={inView ? "visible" : "hidden"}
      variants={variants}
      transition={transition}
    >
      {children}
    </motion.div>
  );
}
