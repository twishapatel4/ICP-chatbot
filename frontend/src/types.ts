// Mirrors icp-chatbot/src/icp_chatbot/api/schemas.py and icp/document.py.

export type Track = "sales" | "hr";

export interface StartRequest {
  company_name: string;
  track: Track;
  website?: string;
  email?: string;
  role?: string;
  industry?: string;
  company_size?: string;
  source?: string;
}

// A LinkedIn option from GET /options/{facet}.
export interface ProfileOption {
  id: string;
  label: string;
  level: number;
  parent: string | null;
  selectable?: boolean; // false: a heading only (e.g. the region "Other territories"); missing means true
}

export type FieldInput = "single" | "multi" | "chips" | "range" | "geo";

export interface CardField {
  slot: string;
  label: string;
  input: FieldInput;
  facet: string | null;
  optional: boolean;
  any_allowed: boolean;
  choices: string[]; // fixed choices of a field without a facet
  // the slot's skip rules: the field is hidden until each controlling slot is answered,
  // and skipped when one of them holds the paired value
  depends_on: [string, string][];
}

export interface Card {
  id: string;
  title: string;
  required: boolean;
  available: boolean; // false while a catalog it needs is missing: leave the card out
  fields: CardField[];
}

// POST /sessions: step 1 is done, the session waits for the step 2 cards.
export interface Step1Response {
  session_id: string;
  step: string;
  track: Track;
  cards: Card[];
}

// GET /sessions/{id}
export interface SessionView {
  session_id: string;
  status: string;
  step: "profile" | "chat";
  track: Track;
  cards: Card[] | null; // while step is "profile"
  answers: Record<string, string[]> | null; // while step is "profile": the last submitted
}

export interface Question {
  text: string;
  kind: string;
  slot: string | null;
  options: string[];
  multi: boolean;
}

export interface Progress {
  percent: number; // 0 to 100: how much of the ICP is gathered
  known: number; // must-ask fields settled so far
  total: number; // must-ask fields that apply to this company
}

export interface Turn {
  session_id: string;
  status: "active" | "completed" | "fallback";
  mode: string;
  questions_asked: number;
  question: Question | null;
  stop_reason: string | null;
  icp_ready: boolean;
  fallback_form: boolean;
  notice: string | null;
  progress?: Progress;
}

export interface FormField {
  key: string;
  label: string;
  description: string;
  choices: string[];
  required: boolean;
}

export interface FieldEntry {
  values: string[];
  confidence: number;
  source: string;
  evidence: string;
  last_verified: string | null;
  requirement: "required" | "preferred" | "none";
}

export interface Criterion {
  key: string;
  label: string;
  weight: number;
  applicable: boolean;
  requirement: "required" | "preferred";
}

export interface Disqualifier {
  field: string;
  operator: "in" | "not_in" | "lt" | "gt" | "eq";
  values: string[];
}

// Mirrors icp/review.py.
export type IcpEdit =
  | { kind: "field"; slot: string; values: string[] }
  | { kind: "weights"; weights: Record<string, number> }
  | { kind: "requirement"; criterion: string; requirement: "required" | "preferred" }
  | { kind: "disqualifiers"; disqualifiers: Disqualifier[] };

export interface FilterEntry {
  facet: string;
  slot: string;
  filter_name?: string | null; // LinkedIn's own name for the filter; fall back to the facet
  ids: string[];
  labels: string[];
  unmapped: string[]; // values that are not a LinkedIn option and will not become a filter
}

export interface IcpDocument {
  track?: Track;
  linkedin_filters?: FilterEntry[];
  sections: Record<string, Record<string, FieldEntry>>;
  scoring_model: { criteria: Criterion[]; disqualifiers: Disqualifier[] };
  summary: string;
  completeness: { tier_a: number; tier_b: number };
  open_questions: string[];
  partial?: boolean; // built before the basics were known, because the user stopped early
}

export interface IcpResponse {
  session_id: string;
  version: number;
  author: string;
  document: IcpDocument;
}
