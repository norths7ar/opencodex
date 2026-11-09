export type SyncFlags = { ok: true; json: boolean } | { ok: false; message: string };

export function parseSyncFlags(args: readonly string[]): SyncFlags {
  const allowed = new Set(["--json", "--restart-codex", "--restart-app-server-only", "--restart-desktop-app"]);
  if (args.some(arg => !allowed.has(arg))) {
    return { ok: false, message: "Unknown ocx sync argument. Use --json, --restart-codex, or --restart-app-server-only." };
  }
  if (args.filter(arg => arg === "--json").length > 1) {
    return { ok: false, message: "ocx sync accepts --json once without a value." };
  }
  return { ok: true, json: args.includes("--json") };
}
