/**
 * Helpers that check Response status before consuming the body.
 * `fetch` also resolves on HTTP 4xx/5xx, which remain failures here.
 */

/** Parse an OK response body; 204 / empty bodies yield `undefined`. */
async function readJsonBody<T>(res: Response): Promise<T | undefined> {
  if (res.status === 204) return undefined;
  // Prefer text() so empty OK bodies are detectable. Fall back to json() for
  // Response-like test doubles that only implement json().
  if (typeof res.text === "function") {
    const text = await res.text();
    if (!text.trim()) return undefined;
    return JSON.parse(text) as T;
  }
  return (await res.json()) as T;
}

export type ManagementError = {
  code: string;
  message: string;
  retryable: boolean;
  fields?: unknown;
};
export class ManagementApiError extends Error {
  readonly status: number;
  readonly detail: ManagementError;
  constructor(status: number, detail: ManagementError) {
    super(detail.message);
    this.name = "ManagementApiError";
    this.status = status;
    this.detail = detail;
  }
}
export async function readManagementError(
  res: Response,
  fallback = `HTTP ${res.status}`,
): Promise<ManagementApiError> {
  let detail: ManagementError = {
    code: "http_error",
    message: fallback,
    retryable: res.status >= 500,
  };
  try {
    const body = (await res.json()) as { error?: Partial<ManagementError> };
    if (body.error && typeof body.error === "object")
      detail = {
        code: typeof body.error.code === "string" ? body.error.code : detail.code,
        message: typeof body.error.message === "string" ? body.error.message : fallback,
        retryable: body.error.retryable === true,
        fields: body.error.fields,
      };
  } catch {
    /* Non-JSON HTTP failures retain the status fallback. */
  }
  return new ManagementApiError(res.status, detail);
}

export async function readJsonOrThrow<T>(
  res: Response,
  fallbackMessage = `HTTP ${res.status}`,
): Promise<T | undefined> {
  if (!res.ok) throw await readManagementError(res, fallbackMessage);
  return readJsonBody<T>(res);
}

export async function readJsonIfOk<T>(res: Response): Promise<T | null | undefined> {
  if (!res.ok) return null;
  try {
    return await readJsonBody<T>(res);
  } catch {
    // Malformed / non-JSON OK bodies must not throw — callers treat null as "no usable payload".
    return null;
  }
}
