import { useState } from "react";
import type { StartRequest } from "../types";

const FIELDS: [keyof StartRequest, string, string][] = [
  ["company_name", "Company name *", "Acme Ltd"],
  ["website", "Website (optional)", "acme.com"],
  ["industry", "Industry (optional)", "Logistics software"],
  ["role", "Your role (optional)", "Business development"],
];

interface Props {
  disabled: boolean;
  onStart: (body: StartRequest) => void;
}

export default function SignupCard({ disabled, onStart }: Props) {
  const [values, setValues] = useState<Partial<Record<keyof StartRequest, string>>>({});

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, string> = {};
    for (const [key] of FIELDS) {
      const v = values[key]?.trim();
      if (v) body[key] = v;
    }
    if (!body.company_name) return;
    onStart(body as unknown as StartRequest);
  }

  return (
    <form className="card" onSubmit={submit}>
      {FIELDS.map(([key, label, placeholder], i) => (
        <label key={key}>
          {label}
          <input
            type="text"
            maxLength={200}
            autoFocus={i === 0}
            placeholder={placeholder}
            disabled={disabled}
            value={values[key] ?? ""}
            onChange={(e) => setValues({ ...values, [key]: e.target.value })}
          />
        </label>
      ))}
      <div className="actions">
        <button className="primary" type="submit" disabled={disabled || !values.company_name?.trim()}>
          Start
        </button>
      </div>
    </form>
  );
}
