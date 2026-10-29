import type { Track } from "./types";

// What the document is called. HR users never see "ICP", "customer" or "lead"; before a role is
// chosen the wording stays neutral.
export const profileName = (track: Track | null) =>
  track === "hr" ? "Ideal Candidate Profile" : track === "sales" ? "Ideal Customer Profile" : "ideal profile";

export const buildNote = (track: Track | null) => `Building your ${profileName(track)}. This can take a minute...`;
