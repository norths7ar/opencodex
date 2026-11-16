import { useState } from "react";
import { setClientResourceData, useKeyedClientResource } from "../../client-resource";
import { DataSurfaceSkeleton } from "../../components/data-surface";
import { Button } from "../../components/button";
import { Notice } from "../../ui";
import { useClientCopy } from "./client-copy";
import { applyDsh, IntegrationApiError, previewDsh, type DshOperation } from "./integration-api";
import ClientDialog from "./ClientDialog";

export default function DshPlanDialog({
  apiBase,
  operation,
  opId,
  configPath,
  onClose,
  onApplied,
}: {
  apiBase: string;
  operation: DshOperation;
  opId?: string;
  configPath: string;
  onClose: () => void;
  onApplied: () => void;
}) {
  const copy = useClientCopy();
  const [confirmDrift, setConfirmDrift] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const key = `lite:dsh-plan:${apiBase}:${operation}:${opId ?? ""}:${confirmDrift}`;
  const resource = useKeyedClientResource(
    key,
    [apiBase, operation, opId, confirmDrift],
    (signal) => previewDsh(apiBase, operation, opId, confirmDrift, signal),
    { staleAfterMs: 0 },
  );
  const plan = resource.data;
  const drift = plan?.refusalReason === "drift_requires_confirm" || confirmDrift;
  const description =
    operation === "apply"
      ? "planApply"
      : operation === "overwrite"
        ? "planOverwrite"
        : operation === "disable"
          ? "planDisable"
          : "planRestore";
  const apply = async () => {
    if (!plan || !plan.canApply) return;
    setPending(true);
    setFailure(null);
    try {
      await applyDsh(apiBase, plan, opId, confirmDrift);
      onApplied();
    } catch (error) {
      if (error instanceof IntegrationApiError && error.detail.plan) {
        setClientResourceData(key, error.detail.plan);
        setFailure(copy("planChanged"));
      } else setFailure(error instanceof Error ? error.message : copy("error"));
    } finally {
      setPending(false);
    }
  };
  return (
    <ClientDialog
      title={copy("planTitle")}
      pending={pending}
      disabled={!plan?.canApply || resource.refreshing || !resource.lastAttemptOk}
      onClose={onClose}
      onConfirm={() => void apply()}
    >
      <p>{copy(description)}</p>
      <code>{configPath}</code>
      {!plan && !resource.error && <DataSurfaceSkeleton label={copy("loading")} rows={3} />}
      {Boolean(resource.error) && (
        <>
          <Notice tone="err">{String(resource.error)}</Notice>
          <Button disabled={resource.refreshing} onClick={() => resource.refresh()}>
            {copy("retry")}
          </Button>
        </>
      )}
      {plan && (
        <>
          {plan.changes.length > 0 && (
            <ul className="client-plan-changes">
              {plan.changes.map((change, index) => (
                <li key={`${change.path}:${index}`}>
                  <code>{change.path}</code>
                </li>
              ))}
            </ul>
          )}
          {!plan.willChange && plan.canApply && <p className="muted">{copy("noChanges")}</p>}
          {!plan.canApply && !drift && (
            <Notice tone="warn">
              {copy(
                plan.refusalReason === "not_installed"
                  ? "notInstalled"
                  : plan.refusalReason === "snapshot_expired"
                    ? "expired"
                    : plan.refusalReason === "superseded_store"
                      ? "missingStore"
                      : "blocked",
              )}
            </Notice>
          )}
          {plan.missingStoreDocument && <pre>{plan.missingStoreDocument}</pre>}
          {drift && (
            <>
              <Notice tone="warn">{copy("drift")}</Notice>
              <label className="client-check">
                <input
                  type="checkbox"
                  checked={confirmDrift}
                  disabled={pending}
                  onChange={(event) => {
                    setConfirmDrift(event.target.checked);
                    setFailure(null);
                  }}
                />
                {copy("allowDrift")}
              </label>
            </>
          )}
        </>
      )}
      {failure && <Notice tone="err">{failure}</Notice>}
    </ClientDialog>
  );
}
