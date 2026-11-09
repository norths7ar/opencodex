import type { ProxyRestartResult } from "./proxy-restart";

export function reportRestartFailure(result: Extract<ProxyRestartResult, { ok: false }>): void {
  if (result.phase === "identity") {
    console.error("Refusing to restart because the running proxy identity could not be attested. Run `ocx status` to inspect the intended proxy.");
  } else if (result.phase === "request") {
    const code = result.error instanceof Error ? result.error.message : "";
    if (code === "restart_capability_unsupported") {
      console.error("The running proxy predates process-bound restart support; no unsafe fallback was attempted.");
      console.error("   After confirming this home owns the proxy, run `ocx stop` and then `ocx start` once.");
    } else if (code === "restart_version_skew") {
      console.error("This CLI and the running proxy have different versions. Run ocx status to confirm its owner, then stop the running instance and start it from the intended installation. No changes were made.");
    } else {
      console.error("Proxy restart request could not be confirmed; no fallback stop/start was attempted. Run `ocx status` to inspect ownership and runtime state.");
    }
  } else if (result.phase === "replacement") {
    console.error("Proxy restart was accepted, but no identity-verified replacement became healthy in time. Run `ocx status` to inspect the replacement.");
  } else {
    console.error("Proxy was not running and the fallback start did not become healthy. Run `ocx status` to inspect startup state.");
  }
}
