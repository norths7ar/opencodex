import { invalidateClientResource, useKeyedClientResource } from "../../client-resource";
import { loadCodex, loadDsh, loadJournal } from "./integration-api";
const codexKey = (base: string, home: string) => ["lite", "codex", base, home].join(":");
const dshKey = (base: string) => ["lite", "dsh", base].join(":");
const journalKey = (base: string) => `lite:dsh-journal:${base}`;
export function useCodexConnection(base: string, home: string, active: boolean) {
  return useKeyedClientResource(
    codexKey(base, home),
    [base, home],
    (signal) => loadCodex(base, home, signal),
    { enabled: active, pollMs: 15000 },
  );
}
export function useDshConnection(base: string, active: boolean) {
  return useKeyedClientResource(dshKey(base), [base], (signal) => loadDsh(base, signal), {
    enabled: active,
    pollMs: 15000,
  });
}
export function useDshJournal(base: string, active: boolean) {
  return useKeyedClientResource(journalKey(base), [base], (signal) => loadJournal(base, signal), {
    enabled: active,
  });
}
export function refreshClient(base: string, client: "codex" | "dsh", home = "") {
  if (client === "codex") {
    invalidateClientResource(codexKey(base, home));
    if (home) invalidateClientResource(codexKey(base, ""));
  } else {
    invalidateClientResource(dshKey(base));
    invalidateClientResource(journalKey(base));
  }
}
