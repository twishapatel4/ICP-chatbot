// Mirrors icp-chatbot/src/icp_chatbot/api/schemas.py and icp/document.py.

export interface StartRequest {
  company_name: string;
  website?: string;
  email?: string;
  role?: string;
  industry?: string;
  company_size?: string;
  source?: string;
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

export interface IcpDocument {
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
