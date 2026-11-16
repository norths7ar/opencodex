import type { ApplicationReceipt } from "./channels";

export function clientApplicationWarning(
  application: ApplicationReceipt | null | undefined,
  fallback: string,
): string | undefined {
  if (application?.status !== "failed") return undefined;
  const failures = Object.entries(application.clients ?? {})
    .filter(([, outcome]) => !outcome.ok)
    .map(([client, outcome]) => {
      const detail = outcome.message || outcome.reason;
      return detail ? `${client}: ${detail}` : client;
    });
  return failures.length ? `${fallback} ${failures.join("; ")}` : (application.message ?? fallback);
}
