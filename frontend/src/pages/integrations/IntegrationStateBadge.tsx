import { useClientCopy } from "./client-copy";
import type { IntegrationState } from "./integration-api";
export default function IntegrationStateBadge({
  state,
}: {
  state: IntegrationState | "unknown" | "unavailable";
}) {
  const copy = useClientCopy();
  const label = state === "unsafe" ? "conflict" : state === "current" ? "connected" : state;
  return (
    <span className="client-state" data-state={state}>
      {copy(label)}
    </span>
  );
}
