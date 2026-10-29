import { useEffect, useState } from "react";
import * as api from "../api";
import type { StartRequest, Track } from "../types";
import PickMenu from "./PickMenu";
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
      .then((all) =>
        setGroups(all.filter((o) => o.level === 1).map((o) => o.label)),
      )
      .catch(() => setGroupsFailed(true));
  }, []);

  // step 1 is asked one screen at a time: role + company, website (optional), industry
  const [screen, setScreen] = useState(0);
  const last = screen === 2;
  const ready = [!!track && !!company.trim(), true, !!industry][screen];

  return (
    <form
      className={`${card} flex flex-col gap-4 w-full max-w-[640px] mx-auto text-left`}
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready || busy) return;
        if (!last) return setScreen(screen + 1);
        onSubmit({
          track: track as Track,
          company_name: company.trim(),
          ...(website.trim() && { website: website.trim() }),
          industry,
        });
      }}
    >
      {screen === 0 && (
        <>
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
          Website (optional)
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
          Industry *
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
          {screen === 1 && !website.trim() ? "Skip" : "Continue"}
        </button>
      </div>
    </form>
  );
}
