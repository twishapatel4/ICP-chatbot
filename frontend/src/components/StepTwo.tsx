import { useEffect, useMemo, useState } from "react";
import * as api from "../api";
import type { Card, CardField, ProfileOption, Track } from "../types";
import { actions, card, chip, chipOn, pillInput, primaryBtn } from "../ui";

export const ANY = "Any";
const MAX_VALUES = 10;
const MAX_CHARS = 80;
const SEARCH_ABOVE = 40; // a facet with more options than this gets a search box, not chips
const draftKey = (id: string) => `icp-step2-draft-${id}`;

type Answers = Record<string, string[]>;

// ---- draft kept per session, so a refresh keeps what was typed ----

function loadDraft(id: string): Answers | null {
  try {
    const raw = localStorage.getItem(draftKey(id));
    return raw ? (JSON.parse(raw) as Answers) : null;
  } catch {
    return null;
  }
}

function saveDraft(id: string, answers: Answers) {
  try {
    localStorage.setItem(draftKey(id), JSON.stringify(answers));
  } catch {
    // storage unavailable: the draft just will not survive a refresh
  }
}

export function clearDraft(id: string) {
  try {
    localStorage.removeItem(draftKey(id));
  } catch {
    // nothing to clear
  }
}

// ---- which fields show ----

// Hidden until every controlling slot is answered; skipped when one holds its skip value.
function fieldShown(f: CardField, answers: Answers): boolean {
  for (const [slot] of f.depends_on) if (!(answers[slot]?.length ?? 0)) return false;
  return !f.depends_on.some(([slot, value]) => (answers[slot] ?? []).some((v) => v.toLowerCase() === value.toLowerCase()));
}

function shownFields(c: Card, answers: Answers): CardField[] {
  return c.available ? c.fields.filter((f) => fieldShown(f, answers)) : [];
}

const answered = (f: CardField, answers: Answers) => (answers[f.slot]?.length ?? 0) > 0;

function missingSlots(cards: Card[], answers: Answers): string[] {
  return cards.flatMap((c) =>
    shownFields(c, answers)
      .filter((f) => c.required && !f.optional && !answered(f, answers))
      .map((f) => f.slot),
  );
}

// ---- options from the LinkedIn catalog ----

const cache = new Map<string, Promise<ProfileOption[]>>();

function useOptions(facet: string | null): ProfileOption[] {
  const [options, setOptions] = useState<ProfileOption[]>([]);
  useEffect(() => {
    if (!facet) return;
    let live = true;
    let pending = cache.get(facet);
    if (!pending) {
      pending = api.getOptions(facet);
      cache.set(facet, pending);
      pending.catch(() => cache.delete(facet)); // a failed load is tried again next time
    }
    pending.then((o) => live && setOptions(o)).catch(() => undefined);
    return () => {
      live = false;
    };
  }, [facet]);
  return options;
}

// ---- small pieces ----

const WRAP = "flex flex-wrap gap-2";

function Chip({ text, on, onClick, disabled, remove }: { text: string; on?: boolean; onClick: () => void; disabled?: boolean; remove?: boolean }) {
  return (
    <button type="button" className={`${chip} py-2 ${on ? chipOn : ""}`} disabled={disabled} aria-pressed={remove ? undefined : !!on} onClick={onClick}>
      {on && !remove ? "✓ " : ""}
      {text}
      {remove ? " ×" : ""}
    </button>
  );
}

function AnyChip({ values, onChange, disabled }: { values: string[]; onChange: (v: string[]) => void; disabled: boolean }) {
  const on = values.includes(ANY);
  return <Chip text={ANY} on={on} disabled={disabled} onClick={() => onChange(on ? [] : [ANY])} />;
}

// Picking something specific clears Any, and Any clears the specific picks.
const without = (values: string[]) => values.filter((v) => v !== ANY);
const toggle = (values: string[], v: string) => (values.includes(v) ? values.filter((x) => x !== v) : [...without(values), v]);

function SearchPicker({
  options,
  values,
  suggest,
  placeholder,
  disabled,
  onToggle,
}: {
  options: ProfileOption[];
  values: string[];
  suggest: ProfileOption[]; // shown before anything is typed
  placeholder: string;
  disabled: boolean;
  onToggle: (label: string) => void;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const needle = q.trim().toLowerCase();
  const results = needle ? options.filter((o) => o.label.toLowerCase().includes(needle)).slice(0, 12) : suggest.slice(0, 20);
  return (
    <div className="relative">
      <input
        className={`${pillInput} block w-full py-2.5 px-[18px]`}
        type="search"
        autoComplete="off"
        placeholder={placeholder}
        aria-label={placeholder}
        disabled={disabled}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
      />
      {open && results.length > 0 && (
        <ul className="absolute z-20 left-0 right-0 mt-1 max-h-60 overflow-y-auto list-none m-0 p-1 bg-panel border border-line rounded-[18px] shadow-soft" role="listbox">
          {results.map((o) => (
            <li key={o.id} role="option" aria-selected={values.includes(o.label)}>
              <button
                type="button"
                className="w-full text-left rounded-[12px] border-0 bg-transparent py-2 px-3 text-[14px] hover:bg-card"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onToggle(o.label);
                  setQ("");
                }}
              >
                {values.includes(o.label) ? "✓ " : ""}
                {o.label}
                {o.parent && <span className="text-muted text-[12px]"> · {o.parent}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FreeChips({ values, disabled, onChange }: { values: string[]; disabled: boolean; onChange: (v: string[]) => void }) {
  const [text, setText] = useState("");
  const add = () => {
    const t = text.trim().slice(0, MAX_CHARS);
    setText("");
    if (t && values.length < MAX_VALUES && !values.some((v) => v.toLowerCase() === t.toLowerCase())) onChange([...values, t]);
  };
  return (
    <div className="flex flex-col gap-2">
      {values.length > 0 && (
        <div className={WRAP}>
          {values.map((v) => (
            <Chip key={v} text={v} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== v))} />
          ))}
        </div>
      )}
      <input
        className={`${pillInput} block w-full py-2.5 px-[18px]`}
        type="text"
        maxLength={MAX_CHARS}
        disabled={disabled || values.length >= MAX_VALUES}
        placeholder="Type and press Enter to add"
        aria-label="Add a value"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add();
          }
        }}
        onBlur={add}
      />
    </div>
  );
}

// ---- field inputs ----

interface FieldProps {
  field: CardField;
  values: string[];
  disabled: boolean;
  onChange: (v: string[]) => void;
}

function ChoiceField({ field, values, disabled, onChange }: FieldProps) {
  const options = useOptions(field.facet);
  const labels = field.facet ? options.map((o) => o.label) : field.choices;
  const single = field.input === "single";
  const big = !!field.facet && options.length > SEARCH_ABOVE;
  const top = options.filter((o) => o.level === 1);
  return (
    <div className="flex flex-col gap-2">
      <div className={WRAP}>
        {field.any_allowed && <AnyChip values={values} onChange={onChange} disabled={disabled} />}
        {big
          ? without(values).map((v) => <Chip key={v} text={v} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== v))} />)
          : labels.map((l) => <Chip key={l} text={l} on={values.includes(l)} disabled={disabled} onClick={() => onChange(single ? [l] : toggle(values, l))} />)}
      </div>
      {big && (
        <SearchPicker
          options={options}
          values={values}
          suggest={top}
          placeholder="Search to add more..."
          disabled={disabled}
          onToggle={(l) => onChange(toggle(values, l))}
        />
      )}
    </div>
  );
}

// Countries come from the catalog; anything else in the list is a city typed by the user.
function GeoField({ field, values, disabled, onChange }: FieldProps) {
  const options = useOptions(field.facet);
  const countryNames = useMemo(() => new Map(options.map((o) => [o.label.toLowerCase(), o.label])), [options]);
  const countries = values.filter((v) => countryNames.has(v.toLowerCase()));
  const cities = values.filter((v) => v !== ANY && !countryNames.has(v.toLowerCase()));
  const [city, setCity] = useState("");
  const addCity = () => {
    const t = city.trim().slice(0, MAX_CHARS);
    setCity("");
    if (t && values.length < MAX_VALUES && !values.some((v) => v.toLowerCase() === t.toLowerCase())) onChange([...values, t]);
  };
  return (
    <div className="flex flex-col gap-2">
      <div className={WRAP}>
        {field.any_allowed && <AnyChip values={values} onChange={onChange} disabled={disabled} />}
        {countries.map((c) => (
          <Chip key={c} text={c} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== c))} />
        ))}
      </div>
      <SearchPicker
        options={options}
        values={countries}
        suggest={[]}
        placeholder="Search for a country..."
        disabled={disabled}
        onToggle={(l) => onChange(toggle(values, l))}
      />
      {countries.length > 0 && (
        <>
          <div className={WRAP}>
            {cities.map((c) => (
              <Chip key={c} text={c} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== c))} />
            ))}
          </div>
          <input
            className={`${pillInput} block w-full py-2.5 px-[18px]`}
            type="text"
            maxLength={MAX_CHARS}
            disabled={disabled}
            placeholder="Add a city (optional), then press Enter"
            aria-label="Add a city"
            value={city}
            onChange={(e) => setCity(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === ",") {
                e.preventDefault();
                addCity();
              }
            }}
            onBlur={addCity}
          />
        </>
      )}
    </div>
  );
}

const SELECT = `${pillInput} py-2.5 px-4`;
const OPEN = "open";

// "3-8" or "3+" for years of experience; "2024" or "2022-2025" for graduation year.
function RangeField({ field, values, disabled, onChange }: FieldProps) {
  const grad = field.slot === "graduation_year";
  const value = values.find((v) => v !== ANY) ?? "";
  const parsed = /^(\d+)(?:(\+)|-(\d+))?$/.exec(value);
  const [from, setFrom] = useState(parsed?.[1] ?? "");
  const [to, setTo] = useState(parsed ? (parsed[2] ? OPEN : (parsed[3] ?? (grad ? parsed[1] : OPEN))) : "");
  const years = useMemo(() => {
    const top = new Date().getFullYear() + 5;
    return Array.from({ length: top - 1990 + 1 }, (_, i) => top - i);
  }, []);

  function emit(f: string, t: string) {
    if (!f) return onChange([]);
    if (grad) return onChange([!t || t === f ? f : `${f}-${t}`]);
    onChange([t === OPEN || !t ? `${f}+` : `${f}-${t}`]);
  }

  const lowers = grad ? years.map(String) : Array.from({ length: 12 }, (_, i) => String(i + 1));
  const uppers = grad
    ? years.filter((y) => y >= Number(from || 0)).map(String)
    : Array.from({ length: 11 }, (_, i) => String(i + 2)).filter((n) => Number(n) > Number(from));
  const lowerText = (n: string) => (!grad && n === "12" ? "12+" : n);

  return (
    <div className="flex flex-col gap-2">
      {field.any_allowed && (
        <div className={WRAP}>
          <AnyChip
            values={values}
            disabled={disabled}
            onChange={(v) => {
              if (v.includes(ANY)) {
                setFrom("");
                setTo("");
              }
              onChange(v);
            }}
          />
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 text-[14px]">
        <label className="flex items-center gap-2">
          From
          <select
            className={SELECT}
            aria-label={`${field.label}, from`}
            disabled={disabled}
            value={from}
            onChange={(e) => {
              const f = e.target.value;
              const keep = grad ? Number(to) >= Number(f) : to !== OPEN && Number(to) > Number(f);
              const t = keep ? to : grad ? f : OPEN;
              setFrom(f);
              setTo(f ? t : "");
              emit(f, t);
            }}
          >
            <option value="">Choose</option>
            {lowers.map((n) => (
              <option key={n} value={n}>
                {lowerText(n)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2">
          To
          <select
            className={SELECT}
            aria-label={`${field.label}, to`}
            disabled={disabled || !from}
            value={to}
            onChange={(e) => {
              setTo(e.target.value);
              emit(from, e.target.value);
            }}
          >
            {!from && <option value="">Choose</option>}
            {uppers.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
            {!grad && <option value={OPEN}>12+ (no upper limit)</option>}
          </select>
        </label>
      </div>
    </div>
  );
}

function FieldInput(props: FieldProps) {
  switch (props.field.input) {
    case "chips":
      return <FreeChips values={props.values} disabled={props.disabled} onChange={props.onChange} />;
    case "range":
      return <RangeField {...props} />;
    case "geo":
      return <GeoField {...props} />;
    default:
      return <ChoiceField {...props} />;
  }
}

// ---- the form ----

interface Props {
  sessionId: string;
  track: Track;
  cards: Card[];
  saved: Answers | null; // the last submitted answers, from the server
  disabled: boolean;
  missing: string[]; // slots the server found missing
  onSubmit: (answers: Answers) => void;
}

export default function StepTwo({ sessionId, track, cards, saved, disabled, missing, onSubmit }: Props) {
  const [answers, setAnswers] = useState<Answers>(() => loadDraft(sessionId) ?? saved ?? {});
  const set = (slot: string, values: string[]) =>
    setAnswers((a) => {
      const next = { ...a, [slot]: values };
      saveDraft(sessionId, next);
      return next;
    });

  const required = missingSlots(cards, answers);
  const titleFor = (c: Card) => (c.id === "geography" && track === "sales" && answers.business_model?.includes("B2C") ? "Where do your customers live?" : c.title);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (required.length || disabled) return;
    // only what is shown goes out; answers behind a skipped branch stay in the draft
    const out: Answers = {};
    for (const c of cards) for (const f of shownFields(c, answers)) if (answered(f, answers)) out[f.slot] = answers[f.slot];
    onSubmit(out);
  }

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      {cards.map((c) => {
        const fields = shownFields(c, answers);
        if (fields.length === 0) return null;
        return (
          <section key={c.id} className={`${card} flex flex-col gap-3`} aria-label={titleFor(c)}>
            <h3 className="m-0 text-[15px]">{titleFor(c)}</h3>
            {fields.map((f) => (
              <div key={f.slot} className="flex flex-col gap-2">
                {fields.length > 1 && (
                  <span className={`text-[12px] font-medium ${missing.includes(f.slot) ? "text-warn" : "text-muted"}`}>
                    {f.label}
                    {f.optional ? " (optional)" : " *"}
                  </span>
                )}
                {fields.length === 1 && missing.includes(f.slot) && <span className="text-[12px] text-warn">Please answer this.</span>}
                <FieldInput field={f} values={answers[f.slot] ?? []} disabled={disabled} onChange={(v) => set(f.slot, v)} />
              </div>
            ))}
          </section>
        );
      })}
      <div className={actions}>
        <button className={primaryBtn} type="submit" disabled={disabled || required.length > 0}>
          Continue
        </button>
        {required.length > 0 && <span className="self-center text-[12px] text-muted">Answer every question above to continue.</span>}
      </div>
    </form>
  );
}
