import { useEffect, useState } from "react";
import * as api from "../api";
import type { StartRequest, Track } from "../types";
import { actions, card, pillInput, primaryBtn } from "../ui";

const ROLES: { value: Track; label: string }[] = [
  { value: "sales", label: "Sales / BDE" },
  { value: "hr", label: "HR" },
];

const INPUT = `${pillInput} block w-full mt-1.5 py-3 px-[18px]`;
const LABEL = "block text-[12px] font-medium text-muted";

interface Props {
  busy: boolean;
  error: string;
  onTrack: (track: Track | null) => void; // lets the page wording follow the chosen role
  onSubmit: (body: StartRequest) => void;
}

export default function StepOne({ busy, error, onTrack, onSubmit }: Props) {
  const [track, setTrack] = useState<Track | "">("");
  const [company, setCompany] = useState("");
  const [website, setWebsite] = useState("");
  const [industry, setIndustry] = useState("");
  const [groups, setGroups] = useState<string[]>([]);
  const [groupsFailed, setGroupsFailed] = useState(false);

  useEffect(() => {
    api
      .getOptions("industry")
      .then((all) => setGroups(all.filter((o) => o.level === 1).map((o) => o.label)))
      .catch(() => setGroupsFailed(true));
  }, []);

  const ready = !!track && !!company.trim() && !!website.trim() && !!industry;

  return (
    <form
      className={`${card} flex flex-col gap-4 w-full max-w-[640px] mx-auto text-left`}
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready || busy) return;
        onSubmit({ track: track as Track, company_name: company.trim(), website: website.trim(), industry });
      }}
    >
      <label className={LABEL}>
        Your role *
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
        Company name *
        <input className={INPUT} type="text" maxLength={200} disabled={busy} autoFocus value={company} onChange={(e) => setCompany(e.target.value)} />
      </label>
      <label className={LABEL}>
        Website *
        <input className={INPUT} type="text" maxLength={200} disabled={busy} placeholder="example.com" value={website} onChange={(e) => setWebsite(e.target.value)} />
      </label>
      <label className={LABEL}>
        Industry *
        <select className={INPUT} value={industry} disabled={busy || groups.length === 0} onChange={(e) => setIndustry(e.target.value)}>
          <option value="">{groupsFailed ? "Could not load industries" : "Choose your industry"}</option>
          {groups.map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </select>
      </label>
      {error && (
        <p className="m-0 text-warn text-[14px]" role="alert">
          {error}
        </p>
      )}
      <div className={actions}>
        <button className={primaryBtn} type="submit" disabled={!ready || busy}>
          Continue
        </button>
      </div>
    </form>
  );
}
