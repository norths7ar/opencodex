/** A failed attempt is offline; an unanswered first check is not evidence of connectivity. */
export function proxyHealthOnline(data: unknown, error: unknown): boolean {
  return error == null && data != null;
}
