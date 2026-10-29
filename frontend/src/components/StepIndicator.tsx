const STEPS = ["About you", "Core profile", "Chat"];

interface Props {
  current: 1 | 2 | 3;
  done?: boolean; // the last step is finished too
}

export default function StepIndicator({ current, done }: Props) {
  return (
    <ol className="flex items-center justify-center gap-2 list-none m-0 p-0 py-2 text-[13px]" aria-label="Steps">
      {STEPS.map((title, i) => {
        const n = i + 1;
        const complete = n < current || (done && n === current);
        const active = n === current && !complete;
        return (
          <li key={title} className="flex items-center gap-2" aria-current={active ? "step" : undefined}>
            {i > 0 && <span className="h-px w-6 sm:w-10 bg-line" aria-hidden="true" />}
            <span
              className={`inline-flex size-6 items-center justify-center rounded-full border text-[12px] font-semibold ${
                active || complete ? "bg-accent text-accent-text border-accent" : "border-line text-muted"
              }`}
            >
              {complete ? "✓" : n}
            </span>
            <span className={active ? "font-semibold" : "text-muted"}>
              <span className="sr-only">Step {n}: </span>
              {title}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
