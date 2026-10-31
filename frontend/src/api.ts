import type { Card, FormField, IcpEdit, IcpResponse, PriorityQuestion, ProfileOption, SessionView, StartRequest, Step1Response, Track, Turn } from "./types";

const TENANT = import.meta.env.VITE_TENANT_ID ?? "demo";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: { "Content-Type": "application/json", "X-Tenant-Id": TENANT },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // an empty or non-JSON body keeps data null
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(res.status, detail ?? res.statusText);
  }
  return data as T;
}

export const startSession = (body: StartRequest) => call<Step1Response>("POST", "/sessions", body);
export const getSession = (id: string) => call<SessionView>("GET", `/sessions/${id}`);
export const getProfileForm = (track: Track) => call<Card[]>("GET", `/profile-form?track=${track}`);
export const getOptions = (facet: string, q = "", limit = 1000) =>
  call<ProfileOption[]>("GET", `/options/${facet}?q=${encodeURIComponent(q)}&limit=${limit}`);
export const submitProfile = (id: string, answers: Record<string, string[]>) =>
  call<Turn>("POST", `/sessions/${id}/profile`, { answers });
export const sendAnswer = (id: string, text: string) =>
  call<Turn>("POST", `/sessions/${id}/answers`, { text });
export const getFallbackForm = (track: Track) => call<FormField[]>("GET", `/fallback-form?track=${track}`);
export const submitFallback = (id: string, answers: Record<string, string[]>) =>
  call<Turn>("POST", `/sessions/${id}/fallback`, { answers });
export const buildIcp = (id: string) => call<IcpResponse>("POST", `/sessions/${id}/icp`);
export const approveIcp = (id: string) => call<IcpResponse>("POST", `/sessions/${id}/approve`);
export const editIcp = (id: string, edit: IcpEdit) =>
  call<IcpResponse>("PATCH", `/sessions/${id}/icp`, edit);
export const getScoringQuestions = (id: string) =>
  call<PriorityQuestion[]>("GET", `/sessions/${id}/scoring-questions`);
export const sendVerdict = (id: string, correct: boolean, comment = "") =>
  call<null>("POST", `/sessions/${id}/verdict`, { correct, comment });
export const undoAnswer = (id: string, text: string, skip: number) =>
  call<Turn>("POST", `/sessions/${id}/undo`, { text, skip });
export const reviseIcp = (id: string, text: string) =>
  call<Turn>("POST", `/sessions/${id}/revise`, { text });

// Voice answer, step 1: audio in, text out. The text then goes to sendAnswer like typed text.
export async function transcribe(id: string, audio: Blob): Promise<string> {
  const res = await fetch(`/api/sessions/${id}/transcribe`, {
    method: "POST",
    headers: { "Content-Type": audio.type || "audio/wav", "X-Tenant-Id": TENANT },
    body: audio,
  });
  let data: { text?: string; detail?: unknown } | null = null;
  try {
    data = await res.json();
  } catch {
    // keep null
  }
  if (!res.ok) throw new ApiError(res.status, data?.detail ?? res.statusText);
  return data?.text ?? "";
}
