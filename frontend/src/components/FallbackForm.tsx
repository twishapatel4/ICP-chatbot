import { useState } from "react";
import type { FormField } from "../types";
import { actions, card, chip, chipOn, chips, pillInput, primaryBtn } from "../ui";

interface Props {
  fields: FormField[];
  disabled: boolean;
  missing: string[];
  onSubmit: (answers: Record<string, string[]>) => void;
}

export default function FallbackForm({ fields, disabled, missing, onSubmit }: Props) {
  const [answers, setAnswers] = useState<Record<string, string[]>>({});

  function toggle(key: string, choice: string) {
    const current = answers[key] ?? [];
    setAnswers({
      ...answers,
      [key]: current.includes(choice) ? current.filter((c) => c !== choice) : [...current, choice],
    });
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const clean: Record<string, string[]> = {};
    for (const [k, v] of Object.entries(answers)) {
      const items = v.map((s) => s.trim()).filter(Boolean);
      if (items.length) clean[k] = items;
    }
    onSubmit(clean);
  }

  return (
    <form className={card} onSubmit={submit}>
      {fields.map((f) => (
        <div key={f.key}>
          <label className={`block text-[12px] font-medium ${missing.includes(f.key) ? "text-warn" : "text-muted"}`}>
            {f.label}
            {f.required ? " *" : ""}
            {f.choices.length === 0 && (
              <input
                className={`${pillInput} block w-full mt-1.5 py-3 px-[18px]`}
                type="text"
                disabled={disabled}
                value={answers[f.key]?.[0] ?? ""}
                onChange={(e) => setAnswers({ ...answers, [f.key]: [e.target.value] })}
              />
            )}
          </label>
          {f.choices.length > 0 && (
            <div className={chips}>
              {f.choices.map((c) => (
                <button
                  type="button"
                  key={c}
                  disabled={disabled}
                  className={`${chip} ${answers[f.key]?.includes(c) ? chipOn : ""}`}
                  onClick={() => toggle(f.key, c)}
                >
                  {c}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
      <div className={actions}>
        <button className={primaryBtn} type="submit" disabled={disabled}>
          Submit
        </button>
      </div>
    </form>
  );
}
