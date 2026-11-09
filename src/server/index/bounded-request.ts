export const GUI_PAIRING_EXCHANGE_BODY_LIMIT = 4 * 1024;

/**
 * Read at most `limit` bytes of a request body, or refuse.
 *
 * Returns null the moment the body is known to exceed `limit`, without retaining the excess.
 * `req.text()` cannot express that: it buffers to completion first, so a caller who omits
 * Content-Length or uses chunked framing decides how much memory the process spends. That
 * matters here because the one caller is an unauthenticated endpoint.
 *
 * limit+1 is the stopping point rather than limit, so a body exactly at the limit is still
 * accepted and only a genuinely over-limit body is rejected.
 */
export async function readBoundedRequestText(req: Request, limit: number): Promise<string | null> {
  const body = req.body;
  if (!body) return "";
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value || value.byteLength === 0) continue;
      total += value.byteLength;
      if (total > limit) return null;
      chunks.push(value);
    }
  } finally {
    // Cancel rather than only releasing the lock: on the reject path the peer may still be
    // sending, and an uncancelled body keeps that transfer alive.
    await reader.cancel().catch(() => {});
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(joined);
}
