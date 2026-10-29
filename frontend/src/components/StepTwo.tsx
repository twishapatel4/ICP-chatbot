import { useEffect, useId, useMemo, useState } from "react";
import * as api from "../api";
import type { Card, CardField, ProfileOption, Track } from "../types";
import PickMenu from "./PickMenu";
import { fuzzyFilter } from "./fuzzy";
import { actions, card, chip, chipOn, pillInput, primaryBtn } from "../ui";

export const ANY = "Any";
const MAX_VALUES = 10;
const MAX_CHARS = 80;
const SEARCH_ABOVE = 40; // a facet with more options than this gets a search box, not chips
const draftKey = (id: string) => `icp-step2-draft-${id}`;

type Answers = Record<string, string[]>;

// ---- draft kept per session, so a refresh keeps what was typed ----

const hasValues = (a: Answers) => Object.values(a).some((v) => Array.isArray(v) && v.length > 0);

// An empty draft is no draft: it must not hide the answers the server kept.
function loadDraft(id: string): Answers | null {
  try {
    const raw = localStorage.getItem(draftKey(id));
    const draft = raw ? (JSON.parse(raw) as Answers) : null;
    return draft && hasValues(draft) ? draft : null;
  } catch {
    return null;
  }
}

function saveDraft(id: string, answers: Answers) {
  try {
    if (hasValues(answers)) localStorage.setItem(draftKey(id), JSON.stringify(answers));
    else localStorage.removeItem(draftKey(id));
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

// The same checks the server makes, so Continue is only enabled for answers it will take.
// `countries` is the country catalog (lower-cased labels), or null while it is not loaded.
function valid(f: CardField, values: string[], countries: Set<string> | null): boolean {
  if (values.length === 0) return false;
  if (values.includes(ANY)) return f.any_allowed && values.length === 1;
  if (values.length > MAX_VALUES || values.some((v) => v.length > MAX_CHARS)) return false;
  if (f.input === "geo") return !!countries && values.some((v) => countries.has(v.toLowerCase()));
  return true;
}

// Visible fields that block Continue: a required one left empty, or any one holding a bad answer.
function problemSlots(cards: Card[], answers: Answers, countries: Set<string> | null): string[] {
  return cards.flatMap((c) =>
    shownFields(c, answers)
      .filter((f) => (answered(f, answers) ? !valid(f, answers[f.slot], countries) : c.required && !f.optional))
      .map((f) => f.slot),
  );
}

// ---- options from the LinkedIn catalog ----

const cache = new Map<string, Promise<ProfileOption[]>>();

interface Loaded {
  options: ProfileOption[];
  error: boolean;
  retry: () => void;
}

function useOptions(facet: string | null): Loaded {
  const [state, setState] = useState<{ facet: string | null; options: ProfileOption[]; error: boolean }>({ facet: null, options: [], error: false });
  const [tries, setTries] = useState(0);
  useEffect(() => {
    if (!facet) return;
    let live = true;
    let pending = cache.get(facet);
    if (!pending) {
      pending = api.getOptions(facet);
      cache.set(facet, pending);
      pending.catch(() => cache.delete(facet)); // a failed load is tried again on Retry
    }
    pending
      .then((options) => live && setState({ facet, options, error: false }))
      .catch(() => live && setState({ facet, options: [], error: true }));
    return () => {
      live = false;
    };
  }, [facet, tries]);
  // never show the previous facet's options while a new one loads
  const now = state.facet === facet ? state : { options: [], error: false };
  return { options: now.options, error: now.error, retry: () => setTries((t) => t + 1) };
}

function LoadError({ retry }: { retry: () => void }) {
  return (
    <p className="m-0 text-[13px] text-warn" role="alert">
      Could not load the options.{" "}
      <button type="button" className="underline border-0 bg-transparent p-0 text-warn" onClick={retry}>
        Retry
      </button>
    </p>
  );
}

// ---- small pieces ----

const WRAP = "flex flex-wrap gap-2";

function Chip({ text, on, onClick, disabled, remove }: { text: string; on?: boolean; onClick: () => void; disabled?: boolean; remove?: boolean }) {
  return (
    <button type="button" className={`${chip} py-2 font-medium ${on ? chipOn : ""}`} disabled={disabled} aria-pressed={remove ? undefined : !!on} onClick={onClick}>
      {on && !remove ? "✓ " : ""}
      {text}
      {remove ? " ×" : ""}
    </button>
  );
}

// A real radio (single choice) or checkbox (multi choice), so the control says which kind of pick it is.
function Choice({ text, kind, name, on, onChange, disabled }: { text: string; kind: "radio" | "checkbox"; name: string; on: boolean; onChange: () => void; disabled?: boolean }) {
  return (
    <label className={`${chip} py-2 flex items-center gap-2 border border-line bg-panel ${on ? "border-accent" : ""} ${disabled ? "opacity-50" : "cursor-pointer"}`}>
      <input className="size-4 accent-accent m-0" type={kind} name={name} checked={on} disabled={disabled} onChange={onChange} />
      {text}
    </label>
  );
}

function AnyChip({ values, onChange, disabled }: { values: string[]; onChange: (v: string[]) => void; disabled: boolean }) {
  const on = values.includes(ANY);
  return <Chip text={ANY} on={on} disabled={disabled} onClick={() => onChange(on ? [] : [ANY])} />;
}

// Picking something specific clears Any, and Any clears the specific picks.
const without = (values: string[]) => values.filter((v) => v !== ANY);
const toggle = (values: string[], v: string) => {
  if (values.includes(v)) return values.filter((x) => x !== v);
  const rest = without(values);
  return rest.length >= MAX_VALUES ? values : [...rest, v];
};
const atCap = (values: string[]) => without(values).length >= MAX_VALUES;
const CAP_HINT = <span className="text-[12px] text-muted">Up to {MAX_VALUES}</span>;

// An ARIA combobox: type to filter, ArrowUp/ArrowDown to move, Enter to pick, Escape to close.
// Enter never submits the form from here. Mouse and touch picks work too.
function SearchPicker({
  options,
  values,
  suggest,
  placeholder,
  disabled,
  full,
  onToggle,
}: {
  options: ProfileOption[];
  values: string[];
  suggest: ProfileOption[]; // shown before anything is typed
  placeholder: string;
  disabled: boolean;
  full: boolean; // no more values may be added
  onToggle: (label: string) => void;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const base = useId();
  const results = q.trim() ? fuzzyFilter(options, q, (o) => o.label).slice(0, 12) : suggest.slice(0, 20);
  const shown = open && results.length > 0;
  const blocked = (o: ProfileOption) => full && !values.includes(o.label);
  const optionId = (i: number) => `${base}-o${i}`;

  function pick(o: ProfileOption | undefined) {
    if (!o || blocked(o)) return;
    onToggle(o.label);
    setQ("");
    setOpen(false);
  }

  return (
    <div className="relative">
      <input
        className={`${pillInput} block w-full py-2.5 px-[18px]`}
        type="text"
        role="combobox"
        aria-expanded={shown}
        aria-controls={`${base}-list`}
        aria-activedescendant={shown ? optionId(active) : undefined}
        aria-autocomplete="list"
        autoComplete="off"
        placeholder={placeholder}
        aria-label={placeholder}
        disabled={disabled}
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            if (!open) return setOpen(true);
            const n = results.length;
            if (n) setActive((a) => (a + (e.key === "ArrowDown" ? 1 : n - 1)) % n);
          } else if (e.key === "Enter") {
            e.preventDefault(); // never submit the form from the search box
            if (shown) pick(results[active]);
          } else if (e.key === "Escape" && open) {
            e.preventDefault();
            setOpen(false);
          }
        }}
      />
      <ul
        id={`${base}-list`}
        role="listbox"
        aria-label={placeholder}
        hidden={!shown}
        className="absolute z-20 left-0 right-0 mt-1 max-h-60 overflow-y-auto list-none m-0 p-1 bg-panel border border-line rounded-[18px] shadow-soft"
      >
        {results.map((o, i) => (
          <li
            key={o.id}
            id={optionId(i)}
            role="option"
            aria-selected={values.includes(o.label)}
            aria-disabled={blocked(o) || undefined}
            className={`rounded-[12px] py-2 px-3 text-[14px] cursor-pointer ${i === active ? "bg-card" : ""} ${blocked(o) ? "opacity-50 cursor-not-allowed" : ""}`}
            onMouseDown={(e) => e.preventDefault()} // keep focus in the input
            onMouseEnter={() => setActive(i)}
            onClick={() => pick(o)}
          >
            {values.includes(o.label) ? "\u2713 " : ""}
            {o.label}
            {o.parent && <span className="text-muted text-[12px]"> · {o.parent}</span>}
          </li>
        ))}
      </ul>
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
      {values.length >= MAX_VALUES && CAP_HINT}
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
  countries: Loaded; // the country catalog, loaded once by the form and shared with geo fields
  onChange: (v: string[]) => void;
}

function ChoiceField({ field, values, disabled, onChange }: FieldProps) {
  const { options, error, retry } = useOptions(field.facet);
  const labels = field.facet ? options.map((o) => o.label) : field.choices;
  const single = field.input === "single";
  const big = !!field.facet && options.length > SEARCH_ABOVE;
  const top = options.filter((o) => o.level === 1);
  const full = !single && atCap(values);
  const name = useId();
  const kind = single ? "radio" : "checkbox";
  const anyOn = values.includes(ANY);
  // a long multi-pick list is a dropdown with the picks listed below, not a wall of boxes
  const menu = !single && !big && !!field.facet;
  const picked = without(values);
  return (
    <div className="flex flex-col gap-2" role={single ? "radiogroup" : "group"} aria-label={field.label}>
      {error && <LoadError retry={retry} />}
      <div className={WRAP}>
        {field.any_allowed && <Choice text={ANY} kind={kind} name={name} on={anyOn} disabled={disabled} onChange={() => onChange(single || !anyOn ? [ANY] : [])} />}
        {menu ? null : big
          ? without(values).map((v) => <Chip key={v} text={v} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== v))} />)
          : labels.map((l) => (
              <Choice
                key={l}
                text={l}
                kind={kind}
                name={name}
                on={values.includes(l)}
                disabled={disabled || (full && !values.includes(l))}
                onChange={() => onChange(single ? [l] : toggle(values, l))}
              />
            ))}
      </div>
      {menu && (
        <>
          <PickMenu
            label={field.label}
            placeholder="Choose an option"
            disabled={disabled || full}
            options={labels.filter((l) => !picked.includes(l))}
            onPick={(l) => onChange(toggle(values, l))}
          />
          {picked.length > 0 && (
            <div className={WRAP}>
              {picked.map((v) => (
                <Chip key={v} text={v} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== v))} />
              ))}
            </div>
          )}
        </>
      )}
      {full && CAP_HINT}
      {big && (
        <SearchPicker
          options={options}
          values={values}
          suggest={top}
          placeholder="Search to add more..."
          disabled={disabled}
          full={full}
          onToggle={(l) => onChange(single ? [l] : toggle(values, l))}
        />
      )}
    </div>
  );
}

// Countries come from the catalog; anything else in the list is a city typed by the user.
function GeoField({ values, disabled, countries: { options, error, retry }, field, onChange }: FieldProps) {
  const countryNames = useMemo(() => new Map(options.map((o) => [o.label.toLowerCase(), o.label])), [options]);
  const countries = values.filter((v) => countryNames.has(v.toLowerCase()));
  const cities = values.filter((v) => v !== ANY && !countryNames.has(v.toLowerCase()));
  const [city, setCity] = useState("");
  const full = atCap(values);
  const [region, setRegion] = useState("");
  // the catalog lists regions (level 1) with their countries (level 2) under them
  const regionNames = useMemo(() => new Set(options.filter((o) => o.level === 1).map((o) => o.label)), [options]);
  const regions = options.filter((o) => o.level === 1).map((o) => o.label);
  const inRegion = options.filter((o) => o.level === 2 && o.parent === region && !countries.includes(o.label));
  const addCity = () => {
    const t = city.trim().slice(0, MAX_CHARS);
    setCity("");
    if (t && !full && !values.some((v) => v.toLowerCase() === t.toLowerCase())) onChange([...values, t]);
  };
  // cities only make sense under a country: removing the last country takes them with it
  const removeCountry = (c: string) => {
    const rest = values.filter((x) => x !== c);
    onChange(rest.some((v) => countryNames.has(v.toLowerCase())) ? rest : []);
  };
  return (
    <div className="flex flex-col gap-2">
      {error && <LoadError retry={retry} />}
      <div className={WRAP}>
        {field.any_allowed && <AnyChip values={values} onChange={onChange} disabled={disabled} />}
      </div>
      <div className="flex flex-wrap gap-2">
        <PickMenu
          label="Region"
          placeholder="Choose a region"
          value={region}
          disabled={disabled || full || options.length === 0}
          options={regions}
          onPick={(r) => {
            setRegion(r);
            // a region on its own means all its countries; naming countries narrows it
            if (!values.includes(r)) onChange(toggle(values, r));
          }}
        />
        <PickMenu
          key={region}
          label="Country"
          placeholder={region ? `Narrow to a country in ${region} (optional)` : "Choose a region first"}
          disabled={disabled || full || !region}
          options={inRegion.map((o) => o.label)}
          onPick={(l) => onChange(toggle(values.filter((v) => v !== region), l))}
        />
      </div>
      {countries.length > 0 && (
        <>
          <div className={WRAP}>
            {countries.map((c) => (
              <Chip key={c} text={regionNames.has(c) ? `${c} (all countries)` : c} on remove disabled={disabled} onClick={() => removeCountry(c)} />
            ))}
            {cities.map((c) => (
              <Chip key={c} text={c} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== c))} />
            ))}
          </div>
          <input
            className={`${pillInput} block w-full py-2.5 px-[18px]`}
            type="text"
            maxLength={MAX_CHARS}
            disabled={disabled || full}
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
      {full && CAP_HINT}
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
  error: string; // any other problem with the last submit
  onSubmit: (answers: Answers) => void;
}

export default function StepTwo({ sessionId, track, cards, saved, disabled, missing, error, onSubmit }: Props) {
  const [answers, setAnswers] = useState<Answers>(() => loadDraft(sessionId) ?? saved ?? {});
  const [flagged, setFlagged] = useState<string[]>(missing);
  useEffect(() => setFlagged(missing), [missing]);
  useEffect(() => saveDraft(sessionId, answers), [sessionId, answers]);
  const set = (slot: string, values: string[]) => {
    setAnswers((a) => ({ ...a, [slot]: values }));
    setFlagged((f) => f.filter((x) => x !== slot)); // an edited field is no longer flagged
  };

  const hasGeo = cards.some((c) => c.fields.some((f) => f.input === "geo"));
  const country = useOptions(hasGeo ? "country" : null);
  const countries = useMemo(() => (hasGeo && country.options.length ? new Set(country.options.map((o) => o.label.toLowerCase())) : null), [hasGeo, country.options]);

  // cities restored without a country (an old draft) cannot be shown or sent: drop them
  useEffect(() => {
    if (!countries) return;
    const strays = cards.flatMap((c) => c.fields).filter((f) => f.input === "geo" && (answers[f.slot]?.length ?? 0) > 0 && !valid(f, answers[f.slot], countries) && !answers[f.slot].includes(ANY) && !answers[f.slot].some((v) => countries.has(v.toLowerCase())));
    if (strays.length) setAnswers((a) => ({ ...a, ...Object.fromEntries(strays.map((f) => [f.slot, []])) }));
  }, [countries, cards, answers]);

  const labels = new Map(cards.flatMap((c) => c.fields.map((f) => [f.slot, f.label] as const)));
  const titleFor = (c: Card) => (c.id === "geography" && track === "sales" && answers.business_model?.includes("B2C") ? "Where do your customers live?" : c.title);

  // one card per screen
  const pages = cards.filter((c) => shownFields(c, answers).length > 0);
  const [page, setPage] = useState(0);
  const at = Math.min(page, pages.length - 1);
  const cur = pages[at];
  const last = at === pages.length - 1;
  // the server flagged something: go to the screen that holds it
  useEffect(() => {
    const i = pages.findIndex((c) => shownFields(c, answers).some((f) => missing.includes(f.slot)));
    if (i >= 0) setPage(i);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missing]);

  const problems = problemSlots(cards, answers, countries);
  const pageProblems = cur ? problemSlots([cur], answers, countries) : [];

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (disabled || pageProblems.length) return;
    if (!last) return setPage(at + 1);
    if (problems.length) return;
    // only what is shown goes out; answers behind a skipped branch stay in the draft
    const out: Answers = {};
    for (const c of cards) for (const f of shownFields(c, answers)) if (answered(f, answers)) out[f.slot] = answers[f.slot];
    onSubmit(out);
  }

  const fields = cur ? shownFields(cur, answers) : [];
  return (
    <form className="flex flex-col gap-4 w-full max-w-[640px] mx-auto" onSubmit={submit}>
      {cur && (
        <>
          <h1 className="mt-2 mb-1 text-center text-[clamp(22px,3vw,28px)] leading-[1.15] font-medium tracking-[-.025em]">{titleFor(cur)}</h1>
          <section key={cur.id} className={`${card} flex flex-col gap-3 text-left`} aria-label={titleFor(cur)}>
            {fields.map((f) => (
              <div key={f.slot} className="flex flex-col gap-2.5 not-first:mt-2 not-first:pt-4 not-first:border-t not-first:border-line">
                {fields.length > 1 && (
                  <span className={`text-[14px] font-medium ${flagged.includes(f.slot) ? "text-warn" : "text-ink"}`}>
                    {f.label}
                    {f.optional ? <span className="font-normal text-muted"> (optional)</span> : <span className="text-muted"> *</span>}
                  </span>
                )}
                <FieldInput field={f} values={answers[f.slot] ?? []} disabled={disabled} countries={country} onChange={(v) => set(f.slot, v)} />
              </div>
            ))}
          </section>
        </>
      )}
      {(flagged.length > 0 || error) && (
        <p className="m-0 text-warn text-[14px]" role="alert">
          {error || "Please check:"}
          {flagged.length > 0 && ` ${flagged.map((s) => labels.get(s) ?? s).join(", ")}`}
        </p>
      )}
      <div className={actions}>
        {at > 0 && (
          <button type="button" disabled={disabled} onClick={() => setPage(at - 1)}>
            Back
          </button>
        )}
        <button className={primaryBtn} type="submit" disabled={disabled || pageProblems.length > 0 || (last && problems.length > 0)}>
          Continue
        </button>
        {pageProblems.length > 0 && <span className="self-center text-[12px] text-muted">Answer every question above to continue.</span>}
      </div>
    </form>
  );
}
