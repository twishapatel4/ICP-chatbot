import type { FormField, IcpEdit, IcpResponse, StartRequest, Turn } from "./types";

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

export const startSession = (body: StartRequest) => call<Turn>("POST", "/sessions", body);
export const sendAnswer = (id: string, text: string) =>
  call<Turn>("POST", `/sessions/${id}/answers`, { text });
export const getFallbackForm = () => call<FormField[]>("GET", "/fallback-form");
export const submitFallback = (id: string, answers: Record<string, string[]>) =>
  call<Turn>("POST", `/sessions/${id}/fallback`, { answers });
export const buildIcp = (id: string) => call<IcpResponse>("POST", `/sessions/${id}/icp`);
export const approveIcp = (id: string) => call<IcpResponse>("POST", `/sessions/${id}/approve`);
export const editIcp = (id: string, edit: IcpEdit) =>
  call<IcpResponse>("PATCH", `/sessions/${id}/icp`, edit);
export const sendVerdict = (id: string, correct: boolean, comment = "") =>
  call<null>("POST", `/sessions/${id}/verdict`, { correct, comment });
export const reviseIcp = (id: string, text: string) =>
  call<Turn>("POST", `/sessions/${id}/revise`, { text });
