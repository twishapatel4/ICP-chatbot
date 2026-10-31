import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import type { StartRequest, Track } from "../types";
import { BotAvatar } from "bot-avatars";
import TextShimmer from "./TextShimmer";
import PickMenu from "./PickMenu";
import { actions, card, pillInput, primaryBtn } from "../ui";

const ROLES: { value: Track; label: string }[] = [
  { value: "sales", label: "Sales / BDE" },
  { value: "hr", label: "HR" },
];

const INPUT = `${pillInput} block w-full mt-1.5 py-3 px-[18px]`;
const LABEL = "block text-[12px] font-medium text-muted";

type Field = "track" | "company" | "website" | "industry";

// website optional per the UI redesign; the spec lists it as required: confirm with the product owner
const STEP1_REQUIRED: Field[] = ["track", "company", "industry"];
const required = (f: Field) => STEP1_REQUIRED.includes(f);
const star = (f: Field) => (required(f) ? " *" : " (optional)");

// step 1 is asked one screen at a time
const SCREENS: { title: string; fields: Field[] }[] = [
  { title: "About you", fields: ["track", "company"] },
  { title: "Your website", fields: ["website"] },
  { title: "Your industry", fields: ["industry"] },
];

interface Props {
  busy: boolean;
  status?: string; // shown inside the card while the server works
  error: string;
  onTrack: (track: Track | null) => void; // lets the page wording follow the chosen role
  onSubmit: (body: StartRequest) => void;
}

export default function StepOne({ busy, status, error, onTrack, onSubmit }: Props) {
  const [track, setTrack] = useState<Track | "">("");
  const [company, setCompany] = useState("");
  const [website, setWebsite] = useState("");
  const [industry, setIndustry] = useState("");
  const [groups, setGroups] = useState<string[]>([]);
  const [parents, setParents] = useState<Record<string, string>>({});
  const [aliases, setAliases] = useState<Record<string, string[]>>({});
  const [levels, setLevels] = useState<Record<string, number>>({});
  const [groupsFailed, setGroupsFailed] = useState(false);

  useEffect(() => {
    api
      .getOptions("industry")
      .then((all) => {
        // every industry, sub-industries too, so a search here finds what Step 2 finds
        const pickable = all.filter((o) => o.selectable !== false);
        setGroups(pickable.map((o) => o.label));
        setAliases(Object.fromEntries(pickable.filter((o) => o.match?.length).map((o) => [o.label, o.match as string[]])));
        setLevels(Object.fromEntries(pickable.map((o) => [o.label, o.level])));
        setParents(Object.fromEntries(pickable.filter((o) => o.parent).map((o) => [o.label, o.parent as string])));
      })
      .catch(() => setGroupsFailed(true));
  }, []);

  const [screen, setScreen] = useState(0);
  const last = screen === SCREENS.length - 1;
  const value: Record<Field, string> = { track, company: company.trim(), website: website.trim(), industry };
  const ok = (fs: Field[]) => fs.every((f) => !required(f) || !!value[f]);
  const ready = ok(SCREENS[screen].fields);
  const allReady = ok(SCREENS.flatMap((s) => s.fields));
  // the button reads "Skip" only on a screen whose fields are all optional and still empty
  const skip = SCREENS[screen].fields.every((f) => !required(f) && !value[f]);

  // move focus to the new screen's heading on Next/Back (not on first load)
  const heading = useRef<HTMLHeadingElement>(null);
  const moved = useRef(false);
  useEffect(() => {
    if (moved.current) heading.current?.focus();
    moved.current = true;
  }, [screen]);

  return (
    <form
      className={`${card} flex flex-col gap-4 w-full max-w-[640px] mx-auto text-left`}
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready || busy) return;
        if (!last) return setScreen(screen + 1);
        if (!allReady) return;
        onSubmit({
          track: track as Track,
          company_name: company.trim(),
          ...(value.website && { website: value.website }),
          industry,
        });
      }}
    >
      <div>
        <p className="m-0 text-[12px] text-muted">
          Question {screen + 1} of {SCREENS.length}
        </p>
        <h2 ref={heading} tabIndex={-1} className="m-0 text-[17px] font-semibold outline-0">
          {SCREENS[screen].title}
        </h2>
      </div>
      {screen === 0 && (
        <>
          <label className={LABEL}>
            Your role{star("track")}
            <select
              className={INPUT}
              value={track}
              disabled={busy}
              onChange={(e) => {
                const t = e.target.value as Track | "";
                setTrack(t);
                onTrack(t || null);
              }}
            >
              <option value="">Choose your role</option>
              {ROLES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <label className={LABEL}>
            Company name{star("company")}
            <input
              className={INPUT}
              type="text"
              maxLength={200}
              disabled={busy}
              autoFocus
              value={company}
              onChange={(e) => setCompany(e.target.value)}
            />
          </label>
        </>
      )}
      {screen === 1 && (
        <label className={LABEL}>
          Website{star("website")}
          <input
            className={INPUT}
            type="text"
            maxLength={200}
            disabled={busy}
            autoFocus
            placeholder="example.com"
            value={website}
            onChange={(e) => setWebsite(e.target.value)}
          />
        </label>
      )}
      {screen === 2 && (
        <div className={LABEL}>
          Industry{star("industry")}
          <div className="mt-1.5">
            <PickMenu
              label="Industry"
              placeholder={
                groupsFailed
                  ? "Could not load industries"
                  : "Choose your industry"
              }
              value={industry}
              disabled={busy || groups.length === 0}
              options={groups}
              detail={parents}
              aliases={aliases}
              levels={levels}
              onPick={setIndustry}
            />
          </div>
        </div>
      )}
      {error && (
        <p className="m-0 text-warn text-[14px]" role="alert">
          {error}
        </p>
      )}
      {busy && status && (
        <div className="flex items-center justify-center gap-2 text-[14px] text-muted" role="status">
          <BotAvatar type="star" state="working" size={28} aria-hidden="true" />
          <TextShimmer>{status}</TextShimmer>
        </div>
      )}
      <div className={actions}>
        {screen > 0 && (
          <button
            type="button"
            disabled={busy}
            onClick={() => setScreen(screen - 1)}
          >
            Back
          </button>
        )}
        <button className={primaryBtn} type="submit" disabled={!ready || busy}>
          {skip ? "Skip" : "Continue"}
        </button>
      </div>
    </form>
  );
}
