// Number that springs to its new value, after motion-primitives' AnimatedNumber
// (https://motion-primitives.com/docs/animated-number).
import { motion, useSpring, useTransform } from "motion/react";
import { useEffect } from "react";

interface Props {
  value: number;
  className?: string;
  mass?: number;
  stiffness?: number;
  damping?: number;
}

export default function AnimatedNumber({ value, className, mass = 0.8, stiffness = 75, damping = 15 }: Props) {
  const spring = useSpring(value, { mass, stiffness, damping });
  const text = useTransform(spring, (v) => Math.round(v).toLocaleString());

  useEffect(() => {
    spring.set(value);
  }, [spring, value]);

  return <motion.span className={className}>{text}</motion.span>;
}
