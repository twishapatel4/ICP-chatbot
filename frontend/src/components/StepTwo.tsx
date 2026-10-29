import { useEffect, useId, useMemo, useRef, useState } from "react";
import * as api from "../api";
import type { Card, CardField, ProfileOption } from "../types";
import { BotAvatar } from "bot-avatars";
import TextShimmer from "./TextShimmer";
import PickMenu from "./PickMenu";
import { fuzzyFilter } from "./fuzzy";
import { actions, card, chip, chipOn, pillInput, primaryBtn } from "../ui";

export const ANY = "Any";
const MAX_CHARS = 80;
const TITLE_PAGE = 50; // job titles are fetched this many at a time as the list is scrolled
const TITLE_MAX = 1000; // the most the server returns for one search
const SEARCH_ABOVE = 40; // a facet with more options than this gets a search box, not chips
const draftKey = (id: string) => `icp-step2-draft-${id}`;

type Answers = Record<string, string[]>;

// ---- draft kept per session, so a refresh keeps what was typed ----

const hasValues = (a: Answers) =>
  Object.values(a).some((v) => Array.isArray(v) && v.length > 0);

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
    if (hasValues(answers))
      localStorage.setItem(draftKey(id), JSON.stringify(answers));
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
  for (const [slot] of f.depends_on)
    if (!(answers[slot]?.length ?? 0)) return false;
  return !f.depends_on.some(([slot, value]) =>
    (answers[slot] ?? []).some((v) => v.toLowerCase() === value.toLowerCase()),
  );
}

function shownFields(c: Card, answers: Answers): CardField[] {
  return c.available ? c.fields.filter((f) => fieldShown(f, answers)) : [];
}

const answered = (f: CardField, answers: Answers) =>
  (answers[f.slot]?.length ?? 0) > 0;

// The same checks the server makes, so Continue is only enabled for answers it will take.
// `countries` is the country catalog (lower-cased labels), or null while it is not loaded.
function valid(
  f: CardField,
  values: string[],
  countries: Set<string> | null,
): boolean {
  if (values.length === 0) return false;
  if (values.includes(ANY)) return f.any_allowed && values.length === 1;
  if (values.some((v) => v.length > MAX_CHARS)) return false;
  if (f.input === "geo")
    return !!countries && values.some((v) => countries.has(v.toLowerCase()));
  return true;
}

// Visible fields that block Continue: a required one left empty, or any one holding a bad answer.
function problemSlots(
  cards: Card[],
  answers: Answers,
  countries: Set<string> | null,
): string[] {
  return cards.flatMap((c) =>
    shownFields(c, answers)
      .filter((f) =>
        answered(f, answers)
          ? !valid(f, answers[f.slot], countries)
          : c.required && !f.optional,
      )
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
  const [state, setState] = useState<{
    facet: string | null;
    options: ProfileOption[];
    error: boolean;
  }>({ facet: null, options: [], error: false });
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
  return {
    options: now.options,
    error: now.error,
    retry: () => setTries((t) => t + 1),
  };
}

function LoadError({
  retry,
  what = "options",
}: {
  retry: () => void;
  what?: string;
}) {
  return (
    <p className="m-0 text-[13px] text-warn" role="alert">
      Could not load the {what}.{" "}
      <button
        type="button"
        className="underline border-0 bg-transparent p-0 text-warn"
        onClick={retry}
      >
        Retry
      </button>
    </p>
  );
}

// ---- small pieces ----

const WRAP = "flex flex-wrap gap-2";

function Chip({
  text,
  on,
  onClick,
  disabled,
  remove,
  soft,
}: {
  text: string;
  on?: boolean;
  onClick: () => void;
  disabled?: boolean;
  remove?: boolean;
  soft?: boolean;
}) {
  // `soft` is the light outline style for secondary picks such as cities, apart from the dark chips
  const look = on ? (soft ? "bg-panel text-ink border-accent" : chipOn) : "";
  return (
    <button
      type="button"
      className={`${on ? "rounded-full py-1 px-3 text-[12px] gap-1" : `${chip} py-2`} font-medium ${look}`}
      disabled={disabled}
      aria-pressed={remove ? undefined : !!on}
      onClick={onClick}
    >
      {on && !remove ? "✓ " : ""}
      {text}
      {remove ? " ×" : ""}
    </button>
  );
}

// A real radio (single choice) or checkbox (multi choice), so the control says which kind of pick it is.
function Choice({
  text,
  kind,
  name,
  on,
  onChange,
  disabled,
}: {
  text: string;
  kind: "radio" | "checkbox";
  name: string;
  on: boolean;
  onChange: () => void;
  disabled?: boolean;
}) {
  return (
    <label
      className={`${chip} py-2 flex items-center gap-2 border border-line bg-panel ${on ? "border-accent" : ""} ${disabled ? "opacity-50" : "cursor-pointer"}`}
    >
      <input
        className="size-4 accent-accent m-0"
        type={kind}
        name={name}
        checked={on}
        disabled={disabled}
        onChange={onChange}
      />
      {text}
    </label>
  );
}

// "Any" is a small checkbox under the control it belongs to, not one of the options.
function AnyToggle({
  values,
  onChange,
  disabled,
}: {
  values: string[];
  onChange: (v: string[]) => void;
  disabled: boolean;
}) {
  return (
    <label className="flex items-center gap-1.5 text-[12px] text-muted cursor-pointer">
      <input
        className="size-3.5 accent-accent m-0"
        type="checkbox"
        checked={values.includes(ANY)}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked ? [ANY] : [])}
      />
      Any
    </label>
  );
}

// Picking something specific clears Any, and Any clears the specific picks.
const without = (values: string[]) => values.filter((v) => v !== ANY);
const toggle = (values: string[], v: string) => {
  if (values.includes(v)) return values.filter((x) => x !== v);
  return [...without(values), v];
};

// An ARIA combobox: type to filter, ArrowUp/ArrowDown to move, Enter to pick, Escape to close.
// Enter never submits the form from here. Mouse and touch picks work too.
function SearchPicker({
  options,
  values,
  suggest,
  placeholder,
  disabled,
  onToggle,
  onQuery,
  onEnd,
  keepOpen,
}: {
  onQuery?: (q: string) => void; // the options come from the server: it is asked on every keystroke
  onEnd?: () => void; // the list was scrolled to its end: ask the server for more
  keepOpen?: boolean; // stay open after a pick, to tick several in a row
  options: ProfileOption[];
  values: string[];
  suggest: ProfileOption[]; // listed, in order, before anything is typed
  placeholder: string;
  disabled: boolean;
  onToggle: (label: string) => void;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const base = useId();
  const searching = q.trim() !== "";
  const results = onQuery ? options : searching ? fuzzyFilter(options, q, (o) => o.label) : suggest;
  const shown = open && results.length > 0;
  const optionId = (i: number) => `${base}-o${i}`;
  useEffect(() => {
    if (shown)
      document
        .getElementById(`${base}-o${active}`)
        ?.scrollIntoView({ block: "nearest" });
  }, [shown, active, base]);

  function pick(o: ProfileOption | undefined) {
    if (!o) return;
    onToggle(o.label);
    if (keepOpen) return;
    setQ("");
    setOpen(false);
  }

  return (
    <div className="relative">
      <div className="sr-only" role="status" aria-live="polite">
        {shown
          ? `${results.length} ${results.length === 1 ? "match" : "matches"}`
          : ""}
      </div>
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
          onQuery?.(e.target.value);
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
            if (n)
              setActive((a) => (a + (e.key === "ArrowDown" ? 1 : n - 1)) % n);
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
        onScroll={(e) => {
          const el = e.currentTarget;
          if (onEnd && el.scrollTop + el.clientHeight >= el.scrollHeight - 40) onEnd();
        }}
        className="absolute z-20 left-0 right-0 mt-1 max-h-60 overflow-y-auto list-none m-0 p-1 bg-panel border border-line rounded-[18px] shadow-soft"
      >
        {results.map((o, i) => (
          <li
            key={o.id}
            id={optionId(i)}
            role="option"
            aria-selected={values.includes(o.label)}
            className={`rounded-[12px] py-2 pr-3 text-[14px] cursor-pointer ${!searching && o.level === 1 ? "font-medium" : ""} ${i === active ? "bg-card" : ""}`}
            style={{ paddingLeft: searching ? 12 : 12 + (o.level - 1) * 16 }} // the whole list, nested by level
            onMouseDown={(e) => e.preventDefault()} // keep focus in the input
            onMouseEnter={() => setActive(i)}
            onClick={() => pick(o)}
          >
            {values.includes(o.label) ? "\u2713 " : ""}
            {o.label}
            {searching && o.parent && (
              <span className="text-muted text-[12px]"> · {o.parent}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

// Job titles: LinkedIn's list is too long to load whole, so the server searches it as you type.
function TitleField({ values, disabled, field, onChange }: FieldProps) {
  const [found, setFound] = useState<ProfileOption[]>([]);
  const [failed, setFailed] = useState(false);
  const asked = useRef({ q: "", limit: TITLE_PAGE });
  const last = useRef(0);
  const timer = useRef<number>();
  const load = (q: string, limit: number) => {
    asked.current = { q, limit };
    const mine = ++last.current;
    api
      .getOptions(field.facet ?? "title", q.trim(), limit)
      .then((r) => mine === last.current && (setFound(r), setFailed(false)))
      .catch(() => mine === last.current && setFailed(true));
  };
  const query = (q: string) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => load(q, TITLE_PAGE), 200);
  };
  // scrolling to the end of the list asks for the next page, up to what the server will give
  const more = () => {
    const { q, limit } = asked.current;
    if (limit < TITLE_MAX && found.length >= limit) load(q, Math.min(limit + TITLE_PAGE, TITLE_MAX));
  };
  useEffect(() => {
    load("", TITLE_PAGE);
    return () => window.clearTimeout(timer.current);
  }, []);
  return (
    <div className="flex flex-col gap-2">
      {failed && <LoadError retry={() => load(asked.current.q, asked.current.limit)} what="job titles" />}
      {values.length > 0 && (
        <div className={WRAP}>
          {values.map((v) => (
            <Chip key={v} text={v} on remove disabled={disabled} onClick={() => onChange(values.filter((x) => x !== v))} />
          ))}
        </div>
      )}
      <SearchPicker
        options={found}
        values={values}
        suggest={found}
        placeholder="Select job titles (type to search)"
        disabled={disabled}
        keepOpen
        onQuery={query}
        onEnd={more}
        onToggle={(l) => onChange(toggle(values, l))}
      />
    </div>
  );
}

function FreeChips({
  values,
  disabled,
  onChange,
}: {
  values: string[];
  disabled: boolean;
  onChange: (v: string[]) => void;
}) {
  const [text, setText] = useState("");
  const add = () => {
    const t = text.trim().slice(0, MAX_CHARS);
    setText("");
    if (t && !values.some((v) => v.toLowerCase() === t.toLowerCase()))
      onChange([...values, t]);
  };
  return (
    <div className="flex flex-col gap-2">
      {values.length > 0 && (
        <div className={WRAP}>
          {values.map((v) => (
            <Chip
              key={v}
              text={v}
              on
              remove
              disabled={disabled}
              onClick={() => onChange(values.filter((x) => x !== v))}
            />
          ))}
        </div>
      )}
      <input
        className={`${pillInput} block w-full py-2.5 px-[18px]`}
        type="text"
        maxLength={MAX_CHARS}
        disabled={disabled}
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
  const name = useId();
  const kind = single ? "radio" : "checkbox";
  // a long multi-pick list is a dropdown with the picks listed below, not a wall of boxes
  const menu = !single && !big && !!field.facet;
  const picked = without(values);
  return (
    <fieldset className="flex flex-col gap-2 min-w-0 m-0 p-0 border-0">
      <legend className="sr-only">{field.label}</legend>
      {error && <LoadError retry={retry} />}
      <div className={WRAP}>
        {menu
          ? null
          : big
            ? without(values).map((v) => (
                <Chip
                  key={v}
                  text={v}
                  on
                  remove
                  disabled={disabled}
                  onClick={() => onChange(values.filter((x) => x !== v))}
                />
              ))
            : labels.map((l) => (
                <Choice
                  key={l}
                  text={l}
                  kind={kind}
                  name={name}
                  on={values.includes(l)}
                  disabled={disabled}
                  onChange={() => onChange(single ? [l] : toggle(values, l))}
                />
              ))}
      </div>
      {!menu && !big && field.any_allowed && (
        <div className="flex justify-end">
          <AnyToggle values={values} onChange={onChange} disabled={disabled} />
        </div>
      )}
      {menu && (
        <>
          <PickMenu
            label={field.label}
            placeholder="Choose an option"
            disabled={disabled}
            options={labels.filter((l) => !picked.includes(l))}
            onPick={(l) => onChange(toggle(values, l))}
          />
          {field.any_allowed && (
            <div className="flex justify-end">
              <AnyToggle
                values={values}
                onChange={onChange}
                disabled={disabled}
              />
            </div>
          )}
          {picked.length > 0 && (
            <div className={WRAP}>
              {picked.map((v) => (
                <Chip
                  key={v}
                  text={v}
                  on
                  remove
                  disabled={disabled}
                  onClick={() => onChange(values.filter((x) => x !== v))}
                />
              ))}
            </div>
          )}
        </>
      )}
      {big && (
        <SearchPicker
          options={options}
          values={values}
          suggest={options}
          placeholder="Search to add more..."
          disabled={disabled}
          onToggle={(l) => onChange(single ? [l] : toggle(values, l))}
        />
      )}
      {big && field.any_allowed && (
        <div className="flex justify-end">
          <AnyToggle values={values} onChange={onChange} disabled={disabled} />
        </div>
      )}
    </fieldset>
  );
}

// The geography answer is regions and countries together, with no cap. A region stands for all
// its countries and stays stored as the region name.
function GeoField({
  values,
  disabled,
  countries: { options, error, retry },
  field,
  onChange,
}: FieldProps) {
  const catalog = useMemo(
    () => new Map(options.map((o) => [o.label.toLowerCase(), o.label])),
    [options],
  );
  const regionNames = useMemo(
    () => new Set(options.filter((o) => o.level === 1).map((o) => o.label)),
    [options],
  );
  // regions to offer as quick-picks: not the ones that are only a heading (selectable: false)
  const quick = useMemo(
    () =>
      options
        .filter((o) => o.level === 1 && o.selectable !== false)
        .map((o) => o.label),
    [options],
  );
  const countryOpts = useMemo(
    () => options.filter((o) => o.level === 2),
    [options],
  );
  const known = (v: string) => catalog.get(v.toLowerCase());
  // cities are not asked any more: one left in an older saved draft is dropped
  const stale = values.some((v) => v !== ANY && !known(v));
  useEffect(() => {
    if (stale && options.length > 0)
      onChange(values.filter((v) => v === ANY || known(v)));
  }, [stale, options.length]);

  // The region the Region button shows and the Country list narrows to: the last one picked, else the
  // first one already in the answer (so a restored draft shows it too).
  const [picked, setPicked] = useState<string | null>(null);
  // the picked region keeps narrowing the Country list even after a country of it replaces its chip
  const region =
    picked ??
    values
      .map(known)
      .find((v) => v && regionNames.has(v) && quick.includes(v)) ??
    "";
  const [showAll, setShowAll] = useState(false);
  const narrowed = !!region && !showAll;

  const countryList = countryOpts.filter(
    (o) =>
      !values.some((v) => v.toLowerCase() === o.label.toLowerCase()) &&
      (!narrowed || o.parent === region),
  );
  const regionOf = useMemo(
    () =>
      Object.fromEntries(
        countryOpts
          .filter((o) => o.parent)
          .map((o) => [o.label, o.parent as string]),
      ),
    [countryOpts],
  );

  const remove = (v: string) => {
    if (v === picked) setPicked(null);
    onChange(values.filter((x) => x !== v)); // only this value; nothing else is touched
  };
  const chipText = (v: string) =>
    regionNames.has(known(v) ?? "") ? `${v} (all countries)` : v;
  const shown = without(values);

  return (
    <fieldset className="flex flex-col gap-2 min-w-0 m-0 p-0 border-0">
      <legend className="sr-only">{field.label}</legend>
      {error && <LoadError retry={retry} />}
      <div className="grid grid-cols-2 gap-2 max-[520px]:grid-cols-1">
        <PickMenu
          label="Region"
          placeholder="Add a region"
          value={region}
          disabled={disabled || quick.length === 0}
          options={quick}
          onPick={(r) => {
            setPicked(r);
            setShowAll(false);
            // adds the region (Any is handled by toggle); countries already chosen stay
            if (!values.includes(r)) onChange(toggle(values, r));
          }}
        />
        <PickMenu
          label="Country"
          placeholder={
            narrowed ? `Add a country in ${region}` : "Add a country"
          }
          disabled={disabled || countryOpts.length === 0}
          options={countryList.map((o) => o.label)}
          detail={narrowed ? undefined : regionOf}
          onPick={(l) => {
            // a country chosen inside a region narrows the answer: the region's "all countries" goes
            const parent = regionOf[l];
            const next = toggle(values, l);
            onChange(
              values.includes(l)
                ? next
                : next.filter((v) => known(v) !== parent),
            );
          }}
        />
      </div>
      {(region || field.any_allowed) && (
        <div className="grid grid-cols-2 gap-2 px-4 text-[12px] max-[520px]:grid-cols-1">
          {field.any_allowed ? (
            <AnyToggle
              values={values}
              disabled={disabled}
              onChange={(v) => {
                setShowAll(false);
                setPicked(null);
                onChange(v);
              }}
            />
          ) : (
            <span />
          )}
          {region ? (
            <button
              type="button"
              className="justify-self-start rounded-none border-0 bg-transparent p-0 text-[12px] font-normal text-muted underline shadow-none hover:translate-y-0"
              aria-pressed={showAll}
              onClick={() => setShowAll(!showAll)}
            >
              {showAll ? `Only countries in ${region}` : "Show all countries"}
            </button>
          ) : null}
        </div>
      )}
      {shown.some(known) && (
        <div className="flex flex-col gap-1.5">
          <span className="text-[12px] text-muted">Regions and countries</span>
          <div className={WRAP}>
            {shown.filter(known).map((v) => (
              <Chip
                key={v}
                text={chipText(v)}
                on
                remove
                disabled={disabled}
                onClick={() => remove(v)}
              />
            ))}
          </div>
        </div>
      )}
    </fieldset>
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
  const [to, setTo] = useState(
    parsed ? (parsed[2] ? OPEN : (parsed[3] ?? (grad ? parsed[1] : OPEN))) : "",
  );
  const years = useMemo(() => {
    const top = new Date().getFullYear() + 5;
    return Array.from({ length: top - 1990 + 1 }, (_, i) => top - i);
  }, []);

  function emit(f: string, t: string) {
    if (!f) return onChange([]);
    if (grad) return onChange([!t || t === f ? f : `${f}-${t}`]);
    onChange([t === OPEN || !t ? `${f}+` : `${f}-${t}`]);
  }

  const lowers = grad
    ? years.map(String)
    : Array.from({ length: 12 }, (_, i) => String(i + 1));
  const uppers = grad
    ? years.filter((y) => y >= Number(from || 0)).map(String)
    : Array.from({ length: 11 }, (_, i) => String(i + 2)).filter(
        (n) => Number(n) > Number(from),
      );
  const lowerText = (n: string) => (!grad && n === "12" ? "12+" : n);

  return (
    <div className="flex flex-col gap-2">
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
              const keep = grad
                ? Number(to) >= Number(f)
                : to !== OPEN && Number(to) > Number(f);
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
      {field.any_allowed && (
        <div className="flex justify-end">
          <AnyToggle
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
    </div>
  );
}

function FieldInput(props: FieldProps) {
  switch (props.field.input) {
    case "chips":
      return (
        <FreeChips
          values={props.values}
          disabled={props.disabled}
          onChange={props.onChange}
        />
      );
    case "range":
      return <RangeField {...props} />;
    case "geo":
      return <GeoField {...props} />;
    case "title":
      return <TitleField {...props} />;
    default:
      return <ChoiceField {...props} />;
  }
}

// ---- the form ----

interface Props {
  sessionId: string;
  cards: Card[];
  saved: Answers | null; // the last submitted answers, from the server
  disabled: boolean;
  missing: string[]; // slots the server found missing
  error: string; // any other problem with the last submit
  status?: string; // shown inside the card while the server works
  onSubmit: (answers: Answers) => void;
}

export default function StepTwo({
  sessionId,
  cards,
  saved,
  disabled,
  missing,
  error,
  status,
  onSubmit,
}: Props) {
  const [answers, setAnswers] = useState<Answers>(
    () => loadDraft(sessionId) ?? saved ?? {},
  );
  const [flagged, setFlagged] = useState<string[]>(missing);
  useEffect(() => setFlagged(missing), [missing]);
  useEffect(() => saveDraft(sessionId, answers), [sessionId, answers]);
  const set = (slot: string, values: string[]) => {
    setAnswers((a) => ({ ...a, [slot]: values }));
    setFlagged((f) => f.filter((x) => x !== slot)); // an edited field is no longer flagged
  };

  const hasGeo = cards.some((c) => c.fields.some((f) => f.input === "geo"));
  const country = useOptions(hasGeo ? "country" : null);
  // the catalog values an answer may use: countries and the regions that can be picked
  const countries = useMemo(
    () =>
      hasGeo && country.options.length
        ? new Set(
            country.options
              .filter((o) => o.selectable !== false)
              .map((o) => o.label.toLowerCase()),
          )
        : null,
    [hasGeo, country.options],
  );

  const labels = new Map(
    cards.flatMap((c) => c.fields.map((f) => [f.slot, f.label] as const)),
  );
  // one card per screen
  const pages = cards.filter((c) => shownFields(c, answers).length > 0);
  const [page, setPage] = useState(0);
  const at = Math.min(page, pages.length - 1);
  const cur = pages[at];
  const last = at === pages.length - 1;
  // the server flagged something: go to the screen that holds it
  useEffect(() => {
    const i = pages.findIndex((c) =>
      shownFields(c, answers).some((f) => missing.includes(f.slot)),
    );
    if (i >= 0) setPage(i);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missing]);

  // move focus to the new screen's heading on Next/Back (not on first load)
  const heading = useRef<HTMLHeadingElement>(null);
  const moved = useRef(false);
  useEffect(() => {
    if (moved.current) heading.current?.focus();
    moved.current = true;
  }, [at]);

  const problems = problemSlots(cards, answers, countries);
  const pageProblems = cur ? problemSlots([cur], answers, countries) : [];
  // on the last screen Continue can wait on an earlier screen: say which
  const earlier = last
    ? pages
        .map((c, i) => ({
          c,
          i,
          bad: problemSlots([c], answers, countries).length > 0,
        }))
        .filter((x) => x.i !== at && x.bad)
    : [];

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (disabled || pageProblems.length) return;
    if (!last) return setPage(at + 1);
    if (problems.length) return;
    // only what is shown goes out; answers behind a skipped branch stay in the draft
    const out: Answers = {};
    for (const c of cards)
      for (const f of shownFields(c, answers))
        if (answered(f, answers)) out[f.slot] = answers[f.slot];
    onSubmit(out);
  }

  const fields = cur ? shownFields(cur, answers) : [];
  return (
    <form
      className="flex flex-col gap-4 w-full max-w-[640px] mx-auto"
      onSubmit={submit}
    >
      {cur && (
        <>
          <p className="m-0 text-center text-[12px] text-muted">
            Question {at + 1} of {pages.length}
          </p>
          <h2
            ref={heading}
            tabIndex={-1}
            className="mt-0 mb-1 text-center outline-0 text-[clamp(22px,3vw,28px)] leading-[1.15] font-medium tracking-[-.025em]"
          >
            {cur.title}
          </h2>
          <section
            key={cur.id}
            className={`${card} flex flex-col gap-3 text-left`}
            aria-label={cur.title}
          >
            {fields.map((f) => (
              <div
                key={f.slot}
                className="flex flex-col gap-2.5 not-first:mt-2 not-first:pt-4 not-first:border-t not-first:border-line"
              >
                {fields.length > 1 && (
                  <span
                    className={`text-[14px] font-medium ${flagged.includes(f.slot) ? "text-warn" : "text-ink"}`}
                  >
                    {f.label}
                    {f.optional ? (
                      <span className="font-normal text-muted">
                        {" "}
                        (optional)
                      </span>
                    ) : (
                      <span className="text-muted"> *</span>
                    )}
                  </span>
                )}
                <FieldInput
                  field={f}
                  values={answers[f.slot] ?? []}
                  disabled={disabled}
                  countries={country}
                  onChange={(v) => set(f.slot, v)}
                />
              </div>
            ))}
            {disabled && status && (
              <div
                className="flex items-center justify-center gap-2 text-[14px] text-muted"
                role="status"
              >
                <BotAvatar
                  type="star"
                  state="working"
                  size={28}
                  aria-hidden="true"
                />
                <TextShimmer>{status}</TextShimmer>
              </div>
            )}
          </section>
        </>
      )}
      {(flagged.length > 0 || error) && (
        <p className="m-0 text-warn text-[14px]" role="alert">
          {error || "Please check:"}
          {flagged.length > 0 &&
            ` ${flagged.map((s) => labels.get(s) ?? s).join(", ")}`}
        </p>
      )}
      <div className={actions}>
        {at > 0 && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => setPage(at - 1)}
          >
            Back
          </button>
        )}
        <button
          className={primaryBtn}
          type="submit"
          disabled={
            disabled || pageProblems.length > 0 || (last && problems.length > 0)
          }
        >
          Continue
        </button>
        {pageProblems.length === 0 && earlier.length > 0 && (
          <span className="self-center text-[12px] text-muted">
            An earlier question needs attention:{" "}
            {earlier.map((x, k) => (
              <span key={x.c.id}>
                {k > 0 && ", "}
                <button
                  type="button"
                  className="underline border-0 bg-transparent p-0 text-[12px] text-warn"
                  onClick={() => setPage(x.i)}
                >
                  {x.c.title}
                </button>
              </span>
            ))}
          </span>
        )}
        {hasGeo && country.error && (
          <LoadError retry={country.retry} what="country list" />
        )}
        {pageProblems.length > 0 && (
          <span className="self-center text-[12px] text-muted">
            Answer every question above to continue.
          </span>
        )}
      </div>
    </form>
  );
}
