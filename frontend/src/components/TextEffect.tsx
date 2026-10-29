// Text reveal, after motion-primitives' TextEffect (https://motion-primitives.com/docs/text-effect):
// the text is split per word, char or line and each piece animates in with a preset.
import { AnimatePresence, motion, type TargetAndTransition, type Transition, type Variants } from "motion/react";
import { type CSSProperties, type ElementType, useEffect, useMemo, useRef } from "react";

export type Preset = "fade" | "fade-in-blur" | "blur" | "scale" | "slide";
type Per = "word" | "char" | "line";

interface Props {
  children: string;
  per?: Per;
  as?: ElementType;
  preset?: Preset;
  delay?: number;
  trigger?: boolean;
  className?: string;
  style?: CSSProperties;
  speedReveal?: number; // 2 = twice as fast
  speedSegment?: number;
  onAnimationComplete?: () => void;
}

const stagger: Record<Per, number> = { char: 0.03, word: 0.05, line: 0.1 };

const container: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: { staggerChildren: 0.05 } },
  exit: { opacity: 0 },
};

const PRESETS: Record<Preset, { item: Variants }> = {
  fade: { item: { hidden: { opacity: 0 }, visible: { opacity: 1 } } },
  "fade-in-blur": {
    item: {
      hidden: { opacity: 0, y: 8, filter: "blur(10px)" },
      visible: { opacity: 1, y: 0, filter: "blur(0px)" },
    },
  },
  blur: { item: { hidden: { opacity: 0, filter: "blur(12px)" }, visible: { opacity: 1, filter: "blur(0px)" } } },
  scale: { item: { hidden: { opacity: 0, scale: 0 }, visible: { opacity: 1, scale: 1 } } },
  slide: { item: { hidden: { opacity: 0, y: 20 }, visible: { opacity: 1, y: 0 } } },
};

function split(text: string, per: Per): string[] {
  if (per === "line") return text.split("\n");
  // keep the spaces as their own pieces so wrapping stays natural
  return per === "word" ? text.split(/(\s+)/) : text.split("");
}

export default function TextEffect({
  children,
  per = "word",
  as: Tag = "span",
  preset = "fade",
  delay = 0,
  trigger = true,
  className,
  style,
  speedReveal = 1,
  speedSegment = 1,
  onAnimationComplete,
}: Props) {
  // After the first reveal, a word that changes (Customer to Candidate) animates in by itself
  const revealed = useRef(false);
  useEffect(() => {
    revealed.current = true;
  }, []);
  const MotionTag = useMemo(() => motion.create(Tag), [Tag]); // a stable component, or every re-render would restart the effect
  const item = PRESETS[preset].item;
  const withTiming = (v: Variants): Variants => ({
    ...v,
    visible: {
      ...(v.visible as TargetAndTransition),
      transition: { duration: 0.3 / speedSegment } as Transition,
    },
  });
  const containerVariants: Variants = {
    ...container,
    visible: {
      opacity: 1,
      transition: { staggerChildren: stagger[per] / speedReveal, delayChildren: delay },
    },
  };

  return (
    <AnimatePresence mode="popLayout">
      {trigger && (
        <MotionTag
          initial="hidden"
          animate="visible"
          exit="exit"
          variants={containerVariants}
          className={className}
          style={style}
          aria-label={children}
          onAnimationComplete={onAnimationComplete}
        >
          {split(children, per).map((seg, i) =>
            /^\s+$/.test(seg) ? (
              seg
            ) : (
              <motion.span
                key={`${per}-${i}-${seg}`}
                aria-hidden="true"
                variants={withTiming(item)}
                {...(revealed.current && { initial: "hidden", animate: "visible" })}
                style={{ display: "inline-block", whiteSpace: per === "line" ? "pre-wrap" : "pre" }}
              >
                {seg}
              </motion.span>
            ),
          )}
        </MotionTag>
      )}
    </AnimatePresence>
  );
}
