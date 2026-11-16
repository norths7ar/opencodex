/** Supported client marks retain their published brand colors. */
export const INTEGRATION_MARKS = {
  codex: "/provider-icons/openai.svg",
  dsh: "/provider-icons/deepseek-harness.svg",
} as const;
export const MASKED_MARKS: ReadonlySet<string> = new Set();
export function markFor(clientId: keyof typeof INTEGRATION_MARKS): string {
  return INTEGRATION_MARKS[clientId];
}
