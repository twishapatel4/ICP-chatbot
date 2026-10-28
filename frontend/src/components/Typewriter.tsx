import { useEffect, useState } from "react";

const MAX_MS = 3500; // long replies speed up so nobody waits more than this

const SPEEDS = { fast: 12, normal: 22, slow: 45 };

interface Props {
  text: string;
  speed?: keyof typeof SPEEDS | number; // ms per character
  variance?: number; // 0-1: how much the timing wobbles, like a real typist
  cursor?: boolean;
  onDone?: () => void;
}

// Types the text out left to right, after the Motion+ Typewriter: a slightly uneven rhythm and a
// blinking cursor. The part not typed yet is rendered invisible, so the bubble keeps its final
// size from the first frame and nothing below it jumps.
export default function Typewriter({ text, speed = "normal", variance = 0.6, cursor = true, onDone }: Props) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const [shown, setShown] = useState(reduce ? text.length : 0);
  const done = shown >= text.length;

  useEffect(() => {
    if (done) {
      onDone?.();
      return;
    }
    const base = Math.min(typeof speed === "number" ? speed : SPEEDS[speed], MAX_MS / text.length);
    const prev = text[shown - 1];
    const pause = prev && ".!?".includes(prev) ? 6 : prev === "," ? 3 : 1; // a beat at punctuation
    const wobble = 1 + (Math.random() * 2 - 1) * variance;
    const id = window.setTimeout(() => setShown((n) => n + 1), Math.max(4, base * wobble * pause));
    return () => window.clearTimeout(id);
  }, [shown, done, text, speed, variance, onDone]);

  return (
    <>
      <span aria-hidden="true">{text.slice(0, shown)}</span>
      {cursor && !done && <span
          className="inline-block w-0.5 h-[1.05em] -mr-0.5 ml-px align-text-bottom bg-current animate-[cursor-blink_1s_steps(1)_infinite] motion-reduce:animate-none"
          aria-hidden="true"
        />}
      <span aria-hidden="true" style={{ visibility: "hidden" }}>
        {text.slice(shown)}
      </span>
      <span className="sr-only">{text}</span>
    </>
  );
}
