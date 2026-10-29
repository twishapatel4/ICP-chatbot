import { useEffect, useId, useRef, useState } from "react";
import { pillInput } from "../ui";
import { fuzzyFilter } from "./fuzzy";

const ROW = 36; // px per option
const LIST_MAX = 240; // the list scrolls past this (max-h-60)
const CHROME = 64; // search box, padding and the gap to the button

interface Props {
  options: string[];
  label: string;
  placeholder: string;
  value?: string; // shown on the button once something is chosen
  disabled?: boolean;
  detail?: Record<string, string>; // a muted note after an option, e.g. the region of a country
  levels?: Record<string, number>; // nesting depth (1 = top): the unsearched list is indented by it, and the note is left out
  onPick: (v: string) => void;
}

// A searchable dropdown that only picks on click or Enter. A native <select> fires change on every
// arrow key, which would add a value per key press.
export default function PickMenu({ options, label, placeholder, value, disabled, detail, levels, onPick }: Props) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const [up, setUp] = useState(false); // not enough room below the button: open above it
  const button = useRef<HTMLButtonElement>(null);
  const base = useId();
  const results = fuzzyFilter(options, q, (o) => o);
  const nested = !!levels && q.trim() === "";

  // keep the highlighted option in view: the country list is 249 long
  useEffect(() => {
    if (open) document.getElementById(`${base}-o${active}`)?.scrollIntoView({ block: "nearest" });
  }, [open, active, base]);

  const close = () => {
    setOpen(false);
    setQ("");
  };
  const pick = (v: string | undefined) => {
    if (v === undefined) return;
    onPick(v);
    close();
    button.current?.focus();
  };

  return (
    <div
      className="relative flex-1 min-w-[180px]"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) close();
      }}
    >
      <button
        ref={button}
        type="button"
        className={`${pillInput} block w-full py-2.5 px-4 text-left text-[14px] font-normal disabled:opacity-50`}
        aria-label={value ? `${label}: ${value}` : label}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => {
          const r = button.current?.getBoundingClientRect();
          if (r) {
            // open below unless the panel would not fit there and there is more room above
            const need = Math.min(options.length * ROW, LIST_MAX) + CHROME;
            const below = window.innerHeight - r.bottom;
            setUp(below < need && r.top > below);
          }
          setActive(0);
          open ? close() : setOpen(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setActive(0);
            setOpen(true);
          }
        }}
      >
        {value || placeholder}
        <span aria-hidden="true" className="float-right">⌄</span>
      </button>
      {/* always mounted, so screen readers announce the count as it changes */}
      <div className="sr-only" role="status" aria-live="polite">
        {open ? (results.length ? `${results.length} ${results.length === 1 ? "match" : "matches"}` : "No matches") : ""}
      </div>
      {open && (
        <div className={`absolute z-20 left-0 right-0 p-1 flex bg-pop border border-line rounded-[18px] shadow-soft ${up ? "bottom-full mb-1 flex-col-reverse" : "mt-1 flex-col"}`}>
          <input
            className={`${pillInput} block w-full py-2 px-3 ${up ? "mt-1" : "mb-1"}`}
            type="text"
            role="combobox"
            aria-expanded
            aria-controls={`${base}-list`}
            aria-activedescendant={results.length ? `${base}-o${active}` : undefined}
            aria-autocomplete="list"
            aria-label={`Search ${label.toLowerCase()}`}
            autoComplete="off"
            placeholder="Search..."
            autoFocus
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setActive(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const n = results.length;
                if (n) setActive((a) => (a + (e.key === "ArrowDown" ? 1 : n - 1)) % n);
              } else if (e.key === "Enter") {
                e.preventDefault(); // never submit the form from the search box
                pick(results[active]);
              } else if (e.key === "Escape") {
                e.preventDefault();
                close();
                button.current?.focus();
              }
            }}
          />
          <ul id={`${base}-list`} role="listbox" aria-label={label} className="max-h-60 overflow-y-auto list-none m-0 p-0">
            {results.map((o, i) => (
              <li
                key={o}
                id={`${base}-o${i}`}
                role="option"
                aria-selected={o === value}
                className={`rounded-[12px] py-2 pr-3 text-[14px] cursor-pointer ${nested && levels?.[o] === 1 ? "font-medium" : ""} ${i === active ? "bg-line" : ""}`}
                style={{ paddingLeft: nested ? 12 + ((levels?.[o] ?? 1) - 1) * 16 : 12 }}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(o)}
              >
                {o}
                {!nested && detail?.[o] && <span className="text-muted text-[12px]"> · {detail[o]}</span>}
              </li>
            ))}
            {results.length === 0 && <li className="py-2 px-3 text-[14px] text-muted">No matches</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
