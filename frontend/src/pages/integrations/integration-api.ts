import type { ClientApplicationOutcome } from "../../contracts/channels";

export type IntegrationState = "absent" | "current" | "stale" | "conflict" | "unsafe";
export interface CodexStatus {
  clientId: "codex";
  state: IntegrationState;
  installed: boolean;
  configPath: string;
  desiredEnabled: boolean;
  disableBlocked: { reason: string; message: string } | null;
  catalogPath: string;
  home: string;
  message: string;
  warning?: string;
  model?: string | null;
}
export interface CodexPreview {
  lastCatalogApplication?: ClientApplicationOutcome | null;
  configPath: string;
  catalogPath: string;
  home: string;
  state: "configured" | "notConfigured" | "conflict";
  model?: string | null;
  baseUrl: string;
  reason?: string;
  integration: CodexStatus;
}
export interface DshStatus {
  lastCatalogApplication?: ClientApplicationOutcome | null;
  clientId: "dsh";
  state: IntegrationState;
  installed: boolean;
  configPath: string;
  appliedAt?: string;
  lastOpId?: string;
  reason?: string;
  error?: string;
  supersededBy?: string;
  missingStoreDocument?: string;
  snapshotCount: number;
  retentionDegraded: boolean;
}
export type DshOperation = "apply" | "overwrite" | "disable" | "restore";
export interface IntegrationMutationPlan {
  version: 1;
  clientId: "dsh";
  operation: DshOperation;
  state: IntegrationState;
  foreignEdit: string;
  changes: { kind: string; path: string }[];
  fingerprint: string;
  canApply: boolean;
  willChange: boolean;
  refusalReason?: string;
  missingStoreDocument?: string;
}
export interface IntegrationJournalRow {
  opId: string;
  clientId: "dsh";
  kind: DshOperation | "refresh";
  at: string;
  configPath: string;
  snapshot: "none" | "stored" | "expired";
  undoable: boolean;
  deletable: boolean;
}
interface ErrorDetail {
  code: string;
  message: string;
  retryable: boolean;
  reason?: string;
  plan?: IntegrationMutationPlan;
}
export class IntegrationApiError extends Error {
  readonly detail: ErrorDetail;
  readonly status: number;
  constructor(detail: ErrorDetail, status: number) {
    super(detail.message);
    this.name = "IntegrationApiError";
    this.detail = detail;
    this.status = status;
  }
}
export async function clientRequest<T>(
  apiBase: string,
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, options);
  const body = await response.json();
  if (!response.ok) throw new IntegrationApiError(body.error as ErrorDetail, response.status);
  return body as T;
}
const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
export const loadCodex = (base: string, home: string, signal?: AbortSignal) =>
  clientRequest<CodexPreview>(
    base,
    `/api/clients/codex${home ? `?home=${encodeURIComponent(home)}` : ""}`,
    { signal },
  );
export const loadDsh = (base: string, signal?: AbortSignal) =>
  clientRequest<DshStatus>(base, "/api/client-integrations/dsh", { signal });
export const loadJournal = (base: string, signal?: AbortSignal) =>
  clientRequest<{ operations: IntegrationJournalRow[] }>(
    base,
    "/api/client-integrations/journal?client=dsh",
    { signal },
  );
export const changeCodex = (base: string, operation: "apply" | "restore" | "sync", home: string) =>
  clientRequest<{ message?: string; warning?: string }>(
    base,
    operation === "sync" ? "/api/sync" : "/api/native-integrations/codex",
    json(operation === "sync" ? "POST" : "PUT", {
      ...(home ? { home } : {}),
      ...(operation === "sync" ? {} : { enabled: operation === "apply" }),
    }),
  );
export function previewDsh(
  base: string,
  operation: DshOperation,
  opId?: string,
  confirmDrift = false,
  signal?: AbortSignal,
) {
  return clientRequest<IntegrationMutationPlan>(
    base,
    operation === "restore"
      ? "/api/client-integrations/restore/preview"
      : "/api/client-integrations/preview",
    { ...json("POST", { clientId: "dsh", operation, opId, confirmDrift }), signal },
  );
}
export function applyDsh(
  base: string,
  plan: IntegrationMutationPlan,
  opId?: string,
  confirmDrift = false,
) {
  const operation = plan.operation;
  return clientRequest<{ ok: true; changed: boolean }>(
    base,
    operation === "restore" ? "/api/client-integrations/restore" : "/api/client-integrations/dsh",
    json(operation === "restore" ? "POST" : "PUT", {
      clientId: "dsh",
      operation,
      opId,
      confirmDrift,
      planFingerprint: plan.fingerprint,
      enabled: operation !== "disable",
      overwriteConflict: operation === "overwrite",
    }),
  );
}
export const deleteJournalEntry = (base: string, opId: string) =>
  clientRequest(base, `/api/client-integrations/journal?opId=${encodeURIComponent(opId)}`, {
    method: "DELETE",
  });
