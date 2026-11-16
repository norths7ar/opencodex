import { useCallback, useState } from "react";
import { Button } from "../../components/button";
import { DataSurfaceSkeleton } from "../../components/data-surface";
import { navigateHash } from "../../hash-routing";
import { Notice } from "../../ui";
import { useCodexRestart } from "../../use-codex-restart";
import { useClientCopy } from "./client-copy";
import { refreshClient, useCodexConnection } from "./client-resources";
import { changeCodex } from "./integration-api";
import ClientDialog from "./ClientDialog";
import IntegrationStateBadge from "./IntegrationStateBadge";

type Operation = "apply" | "restore";
export default function CodexConnection({ apiBase, active }: { apiBase: string; active: boolean }) {
  const copy = useClientCopy();
  const [home, setHome] = useState("");
  const [target, setTarget] = useState("");
  const [operation, setOperation] = useState<Operation | null>(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "ok" | "err" | "warn"; text: string } | null>(
    null,
  );
  const report = useCallback(
    (text: string, tone: "ok" | "err" | "warn") => setFeedback({ text, tone }),
    [],
  );
  const restart = useCodexRestart(apiBase, { report });
  const resource = useCodexConnection(apiBase, target, active);
  const preview = resource.data;
  const draftChanged = target !== home.trim();
  const mutate = async (selected: Operation | "sync") => {
    setBusy(true);
    setFeedback(null);
    try {
      const result = await changeCodex(apiBase, selected, target);
      setOperation(null);
      setFeedback({ tone: result.warning ? "warn" : "ok", text: result.warning || copy("done") });
    } catch (error) {
      setFeedback({ tone: "err", text: error instanceof Error ? error.message : copy("error") });
    } finally {
      setBusy(false);
      refreshClient(apiBase, "codex", target);
    }
  };
  return (
    <section className="client-panel">
      <header className="client-section-heading">
        <h3>{copy("codex")}</h3>
        <IntegrationStateBadge
          state={preview?.integration.state ?? (resource.error ? "unavailable" : "unknown")}
        />
        <Button
          variant="ghost"
          disabled={busy || resource.refreshing}
          onClick={() => resource.refresh()}
        >
          {copy("refresh")}
        </Button>
      </header>
      {!preview && !resource.error && <DataSurfaceSkeleton label={copy("loading")} rows={5} />}
      {Boolean(resource.error) && <Notice tone="err">{String(resource.error)}</Notice>}
      {preview && (
        <>
          <dl className="client-facts">
            <div>
              <dt>{copy("model")}</dt>
              <dd>{preview.model || copy("noModel")}</dd>
            </div>
            <div>
              <dt>{copy("gateway")}</dt>
              <dd>
                <code>{preview.baseUrl}</code>
              </dd>
            </div>
          </dl>
          {preview.integration.message && (
            <Notice tone="warn">{preview.integration.message}</Notice>
          )}
          {preview.integration.warning && (
            <Notice tone="warn">{preview.integration.warning}</Notice>
          )}
          <div className="client-actions">
            <Button
              variant="primary"
              disabled={
                busy ||
                draftChanged ||
                resource.refreshing ||
                !resource.lastAttemptOk ||
                preview.state === "conflict" ||
                !preview.model
              }
              onClick={() => {
                setFeedback(null);
                setOperation("apply");
              }}
            >
              {copy("apply")}
            </Button>
            <Button
              disabled={busy || draftChanged || preview.state !== "configured"}
              onClick={() => void mutate("sync")}
            >
              {copy("sync")}
            </Button>
            <Button
              variant="ghost"
              disabled={
                busy ||
                draftChanged ||
                (!preview.integration.desiredEnabled && preview.state !== "configured")
              }
              onClick={() => {
                setFeedback(null);
                setOperation("restore");
              }}
            >
              {copy("disconnect")}
            </Button>
          </div>
          <div className="client-actions">
            <Button variant="ghost" onClick={() => navigateHash("models")}>
              {copy("models")}
            </Button>
            <Button variant="ghost" onClick={() => navigateHash("subagents")}>
              {copy("roles")}
            </Button>
            {!target && (
              <Button
                variant="ghost"
                disabled={busy || restart.restarting}
                onClick={() => void restart.restart()}
              >
                {copy("restartCodex")}
              </Button>
            )}
          </div>
          <details className="client-files">
            <summary>{copy("files")}</summary>
            <dl className="client-facts">
              <div>
                <dt>{copy("configPath")}</dt>
                <dd>
                  <code>{preview.configPath}</code>
                </dd>
              </div>
              <div>
                <dt>{copy("catalogPath")}</dt>
                <dd>
                  <code>{preview.catalogPath}</code>
                </dd>
              </div>
            </dl>
          </details>
        </>
      )}
      <form
        className="client-home"
        onSubmit={(event) => {
          event.preventDefault();
          setTarget(home.trim());
          setFeedback(null);
        }}
      >
        <label htmlFor="client-codex-home">{copy("home")}</label>
        <div className="client-home-controls">
          <input
            id="client-codex-home"
            className="input"
            value={home}
            onChange={(event) => setHome(event.target.value)}
            disabled={busy}
            placeholder={copy("homeDefault")}
            aria-describedby="client-codex-home-hint"
          />
          <Button type="submit" disabled={busy}>
            {copy("preview")}
          </Button>
        </div>
        <p id="client-codex-home-hint" className="muted">
          {copy(draftChanged ? "previewRequired" : "homeHint")}
        </p>
      </form>
      {feedback && !operation && <Notice tone={feedback.tone}>{feedback.text}</Notice>}
      {active && operation && preview && (
        <ClientDialog
          title={copy(operation === "apply" ? "applyCodex" : "restoreCodex")}
          pending={busy}
          onClose={() => setOperation(null)}
          onConfirm={() => void mutate(operation)}
        >
          <p>{copy(operation === "apply" ? "applyCodexBody" : "restoreCodexBody")}</p>
          <code>{preview.configPath}</code>
          {feedback && <Notice tone={feedback.tone}>{feedback.text}</Notice>}
        </ClientDialog>
      )}
    </section>
  );
}
