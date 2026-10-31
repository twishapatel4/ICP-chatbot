import { useMemo, useState } from "react";
import type { CardField, FormField } from "../types";
import { FieldInput, useOptions, valid } from "./StepTwo";
import { actions, card, primaryBtn } from "../ui";

interface Props {
  fields: FormField[];
  disabled: boolean;
  missing: string[];
  onSubmit: (answers: Record<string, string[]>) => void;
}

// The backup questionnaire asks with the same controls and checks as step 2: dropdowns and pickers
// for LinkedIn options, regions and countries, ranges, "Any", fixed choices and free text.
const asCardField = (f: FormField): CardField => ({
  slot: f.key,
  label: f.label,
  input:
    f.input ??
    (f.choices.length > 0 ? "multi" : f.facet === "title" ? "title" : "chips"),
  facet: f.facet ?? null,
  optional: !f.required,
  any_allowed: !!f.any_allowed,
  choices: f.choices,
  depends_on: [],
});

export default function FallbackForm({
  fields,
  disabled,
  missing,
  onSubmit,
}: Props) {
  const [answers, setAnswers] = useState<Record<string, string[]>>({});
  const cardFields = useMemo(() => fields.map(asCardField), [fields]);
  const hasGeo = cardFields.some((f) => f.input === "geo");
  const country = useOptions(hasGeo ? "country" : null);
  const countries = useMemo(
    () =>
      hasGeo && country.options.length
        ? new Set(country.options.map((o) => o.label.toLowerCase()))
        : null,
    [hasGeo, country.options],
  );

  // a required field left empty, or any field holding an answer the server would not take
  const problems = cardFields
    .filter((f) => {
      const values = answers[f.slot] ?? [];
      return values.length > 0 ? !valid(f, values, countries) : !f.optional;
    })
    .map((f) => f.slot);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (problems.length || disabled) return;
    const clean: Record<string, string[]> = {};
    for (const [k, v] of Object.entries(answers)) {
      const items = v.map((s) => s.trim()).filter(Boolean);
      if (items.length) clean[k] = items;
    }
    onSubmit(clean);
  }

  return (
    <form
      className={`${card} flex flex-col gap-5 w-full max-w-[640px] mx-auto text-left`}
      onSubmit={submit}
    >
      {cardFields.map((f) => (
        <div
          key={f.slot}
          className="flex flex-col gap-2 not-first:pt-4 not-first:border-t not-first:border-line"
        >
          <span className="text-[14px] font-medium text-ink">
            {f.label}
            {f.optional ? (
              <span className="font-normal text-muted"> (optional)</span>
            ) : (
              <span className="text-muted"> *</span>
            )}
            {missing.includes(f.slot) && (
              <span className="ml-2 rounded-full border border-warn py-px px-2 text-[11px] font-normal text-warn">
                Needed
              </span>
            )}
          </span>
          <FieldInput
            field={f}
            values={answers[f.slot] ?? []}
            disabled={disabled}
            countries={country}
            onChange={(v) => setAnswers((a) => ({ ...a, [f.slot]: v }))}
          />
        </div>
      ))}
      <div className={actions}>
        <button
          className={primaryBtn}
          type="submit"
          disabled={disabled || problems.length > 0}
        >
          Submit
        </button>
        {problems.length > 0 && (
          <span className="self-center text-[12px] text-muted">
            Answer every required question to submit.
          </span>
        )}
      </div>
    </form>
  );
}
