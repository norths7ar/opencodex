import { useState } from "react";
import { DataSurfaceSkeleton } from "../../components/data-surface";
import { Button } from "../../components/button";
import { Notice } from "../../ui";
import { useClientCopy } from "./client-copy";
import { refreshClient, useDshConnection, useDshJournal } from "./client-resources";
import {
  deleteJournalEntry,
  type DshOperation,
  type IntegrationJournalRow,
} from "./integration-api";
import ClientDialog from "./ClientDialog";
import DshPlanDialog from "./DshPlanDialog";
import IntegrationStateBadge from "./IntegrationStateBadge";
import { RollbackHistory } from "./RollbackHistory";

export default function DshConnection({ apiBase, active }: { apiBase: string; active: boolean }) {
  const copy = useClientCopy();
  const resource = useDshConnection(apiBase, active);
  const journal = useDshJournal(apiBase, active);
  const [operation, setOperation] = useState<{
    kind: DshOperation;
    row?: IntegrationJournalRow;
  } | null>(null);
  const [deleting, setDeleting] = useState<IntegrationJournalRow | null>(null);
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const status = resource.data;
  const applied = () => {
    setOperation(null);
    refreshClient(apiBase, "dsh");
    setFeedback({ tone: "ok", text: copy("done") });
  };
  const remove = async () => {
    if (!deleting) return;
    setPending(true);
    setFeedback(null);
    try {
      await deleteJournalEntry(apiBase, deleting.opId);
      setDeleting(null);
      refreshClient(apiBase, "dsh");
      setFeedback({ tone: "ok", text: copy("done") });
    } catch (error) {
      setFeedback({ tone: "err", text: error instanceof Error ? error.message : copy("error") });
    } finally {
      setPending(false);
    }
  };
  return (
    <section className="client-panel">
      <header className="client-section-heading">
        <h3>{copy("dsh")}</h3>
        <IntegrationStateBadge
          state={status?.state ?? (resource.error ? "unavailable" : "unknown")}
        />
        <Button
          variant="ghost"
          disabled={resource.refreshing}
          onClick={() => refreshClient(apiBase, "dsh")}
        >
          {copy("refresh")}
        </Button>
      </header>
      {!status && !resource.error && <DataSurfaceSkeleton label={copy("loading")} rows={5} />}
      {Boolean(resource.error) && <Notice tone="err">{String(resource.error)}</Notice>}
      {status && (
        <>
          <dl className="client-facts">
            <div>
              <dt>{copy("configPath")}</dt>
              <dd>
                <code>{status.supersededBy || status.configPath}</code>
              </dd>
            </div>
            {status.appliedAt && (
              <div>
                <dt>{copy("lastUpdated")}</dt>
                <dd>
                  <time dateTime={status.appliedAt}>
                    {new Date(status.appliedAt).toLocaleString()}
                  </time>
                </dd>
              </div>
            )}
          </dl>
          {status.error && <Notice tone="warn">{status.error}</Notice>}
          {!status.installed && <Notice tone="warn">{copy("notInstalled")}</Notice>}
          {status.missingStoreDocument && (
            <Notice tone="warn">
              {copy("missingStore")}
              <pre>{status.missingStoreDocument}</pre>
            </Notice>
          )}
          <div className="client-actions">
            <Button
              variant="primary"
              disabled={
                !status.installed ||
                resource.refreshing ||
                !resource.lastAttemptOk ||
                status.state === "unsafe"
              }
              onClick={() => {
                setFeedback(null);
                setOperation({ kind: status.state === "conflict" ? "overwrite" : "apply" });
              }}
            >
              {copy(
                status.state === "conflict"
                  ? "overwrite"
                  : status.state === "current" || status.state === "stale"
                    ? "sync"
                    : "apply",
              )}
            </Button>
            {status.lastOpId && (
              <Button
                variant="ghost"
                disabled={resource.refreshing}
                onClick={() => {
                  setFeedback(null);
                  setOperation({ kind: "disable" });
                }}
              >
                {copy("disconnect")}
              </Button>
            )}
          </div>
          <p className="muted client-help">{copy("dshAuto")}</p>
        </>
      )}
      {feedback && !deleting && <Notice tone={feedback.tone}>{feedback.text}</Notice>}
      {Boolean(journal.error) && <Notice tone="err">{String(journal.error)}</Notice>}
      {!journal.data && !journal.error && <DataSurfaceSkeleton label={copy("loading")} rows={2} />}
      {journal.data && (
        <RollbackHistory
          rows={journal.data.operations}
          onRestore={(row) => {
            setFeedback(null);
            setOperation({ kind: "restore", row });
          }}
          onDelete={(row) => {
            setFeedback(null);
            setDeleting(row);
          }}
        />
      )}
      {active && operation && (
        <DshPlanDialog
          key={`${operation.kind}:${operation.row?.opId ?? ""}`}
          apiBase={apiBase}
          operation={operation.kind}
          opId={operation.row?.opId}
          configPath={operation.row?.configPath || status?.supersededBy || status?.configPath || ""}
          onClose={() => setOperation(null)}
          onApplied={applied}
        />
      )}
      {active && deleting && (
        <ClientDialog
          title={copy("planDelete")}
          pending={pending}
          onClose={() => setDeleting(null)}
          onConfirm={() => void remove()}
        >
          <p>{copy("deleteBody")}</p>
          <code>{deleting.configPath}</code>
          {feedback && <Notice tone={feedback.tone}>{feedback.text}</Notice>}
        </ClientDialog>
      )}
    </section>
  );
}
