import { beginApiSessionHandoff } from "./api";
import { type ApiTarget } from "./api-targets";
import { discardResponseBody, withFetchDeadline } from "./bounded-fetch";

const PAIRING_CODE = /^ocx_pair_[A-Za-z0-9_-]{43}$/;
let pairingDeadlineMs = 10_000;
export function setPairingDeadlineForTests(ms: number): void {
  pairingDeadlineMs = ms;
}

export class PairingError extends Error {
  readonly kind:
    | "invalid-code"
    | "refused"
    | "origin-denied"
    | "unreachable"
    | "request-failed"
    | "invalid-response";
  constructor(kind: PairingError["kind"]) {
    super(`pairing_${kind}`);
    this.kind = kind;
    this.name = "PairingError";
  }
}

/**
 * Exchange a pairing code for a session on the selected target's plane.
 *
 * Separate module from the form that calls it so neither file mixes a component export with
 * a plain one. That mix is what `react-refresh/only-export-components` flags, and the two
 * have no reason to share a file: the transport is testable without React and the form
 * owns the pending handoff's validation and retry lifetime.
 */
export async function submitConnectPairing(
  target: ApiTarget,
  grant: string,
  fetchImpl?: typeof fetch,
  signal?: AbortSignal,
): Promise<ReturnType<typeof beginApiSessionHandoff>> {
  signal?.throwIfAborted();
  const code = grant.trim();
  if (!PAIRING_CODE.test(code)) throw new PairingError("invalid-code");
  const send = fetchImpl ?? ((input, init) => window.fetch(input, init));
  // Capture authority before either response headers or body can yield to another login.
  const handoff = beginApiSessionHandoff(target.id);
  return withFetchDeadline(pairingDeadlineMs, signal, async (deadline) => {
    let response: Response;
    try {
      response = await send(target.bootstrapPath, {
        method: "POST",
        signal: deadline,
        headers: { "Content-Type": "application/json", Accept: "text/html" },
        body: JSON.stringify({ grant: code }),
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new PairingError("unreachable");
    }
    if (!response.ok) {
      discardResponseBody(response);
      if (response.status === 401) throw new PairingError("refused");
      if (response.status === 403) throw new PairingError("origin-denied");
      throw new PairingError("request-failed");
    }
    let html: string;
    try {
      html = await response.text();
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new PairingError("invalid-response");
    }
    deadline.throwIfAborted();
    if (!handoff.install(html)) {
      handoff.rollback();
      throw new PairingError("invalid-response");
    }
    return handoff;
  }).catch((error) => {
    handoff.rollback();
    signal?.throwIfAborted();
    if (error instanceof PairingError) throw error;
    throw new PairingError("unreachable");
  });
}
