import { useCallback, useState } from "react";
import { useKeyedClientResource } from "../../client-resource";
import { useI18n, useT } from "../../i18n/shared";
import { Notice } from "../../ui";
import { useCodexRestart } from "../../use-codex-restart";
import ConsequenceDialog from "./ConsequenceDialog";
import { apiErrorMessage } from "../../api-error";
import { useCodexRestoreCopy } from "./codex-restore-copy";
import { navigateHash } from "../../hash-routing";
import "./codex-connection.css";

interface CodexPreview {
  configPath: string;
  catalogPath?: string;
  state: string;
  model?: string;
  baseUrl?: string;
  reason?: string;
  integration?: { desiredEnabled?: boolean; message?: string };
}

/** The native Codex integration lives in the gateway, not in a desktop-shell bridge. */
export default function CodexConnection({ apiBase, active }: { apiBase: string; active: boolean }) {
  const t = useT();
  const { locale } = useI18n();
  const restoreCopy = useCodexRestoreCopy();
  const zh = locale === "zh" || locale === "zh-TW";
  const [home, setHome] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [restore, setRestore] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "ok" | "err" | "warn"; text: string } | null>(
    null,
  );
  const report = useCallback(
    (text: string, tone: "ok" | "err" | "warn") => setFeedback({ text, tone }),
    [],
  );
  const resource = useKeyedClientResource<CodexPreview>(
    `codex-connection:${apiBase}:${target}`,
    [apiBase, target],
    async (signal) => {
      const response = await fetch(
        `${apiBase}/api/clients/codex${target ? `?home=${encodeURIComponent(target)}` : ""}`,
        { signal },
      );
      if (!response.ok)
        throw new Error(await apiErrorMessage(response, t("integrations.error.load")));
      return response.json();
    },
    { enabled: active },
  );
  const restart = useCodexRestart(apiBase, { report });
  const mutate = async (operation: "apply" | "restore" | "sync") => {
    setBusy(true);
    setFeedback(null);
    try {
      const response = await fetch(
        `${apiBase}${operation === "sync" ? "/api/sync" : "/api/native-integrations/codex"}`,
        {
          method: operation === "sync" ? "POST" : "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...(target ? { home: target } : {}),
            ...(operation !== "sync" ? { enabled: operation === "apply" } : {}),
          }),
        },
      );
      if (!response.ok)
        throw new Error(await apiErrorMessage(response, t("integrations.error.load")));
      await response.json();
      resource.refresh();
      setFeedback({
        tone: "ok",
        text: zh
          ? operation === "restore"
            ? "已恢复 Codex 配置。"
            : operation === "sync"
              ? "已同步 Codex 模型目录。"
              : "已应用 Codex 接入配置。"
          : operation === "restore"
            ? "Codex configuration restored."
            : operation === "sync"
              ? "Codex model catalog synchronized."
              : "Codex connection applied.",
      });
      setRestore(false);
    } catch (error) {
      resource.refresh();
      const message = error instanceof Error ? error.message : String(error);
      setFeedback({ tone: "err", text: message });
      if (operation === "restore") throw error;
    } finally {
      setBusy(false);
    }
  };
  const preview = resource.data;
  const connected = preview?.state === "configured";
  const stateLabel = connected
    ? zh
      ? "已接入"
      : "Connected"
    : preview?.state === "notConfigured"
      ? zh
        ? "未接入"
        : "Not connected"
      : (preview?.reason ?? preview?.state);
  return (
    <section className="codex-connection" aria-labelledby="codex-integration-title">
      <header className="codex-connection-head">
        <div>
          <h3 id="codex-integration-title">{t("integrations.codex.title")}</h3>
          <p className="muted">
            {zh
              ? "让 Codex 通过本机网关使用已配置的供应商和模型。"
              : "Connect Codex to the providers and models configured in your local gateway."}
          </p>
        </div>
        {preview && (
          <span className={`badge ${connected ? "badge-green" : "badge-muted"}`}>{stateLabel}</span>
        )}
      </header>
      {resource.loading && !preview && <div className="min-h-64" aria-busy="true" />}
      {Boolean(resource.error) && <Notice tone="err">{String(resource.error)}</Notice>}
      {preview && (
        <>
          <div className="codex-connection-card">
            <dl className="codex-connection-summary">
              <div>
                <dt>{zh ? "默认模型" : "Default model"}</dt>
                <dd>
                  <code>{preview.model || (zh ? "尚未选择" : "Not selected")}</code>
                </dd>
              </div>
              <div>
                <dt>{zh ? "网关地址" : "Gateway address"}</dt>
                <dd>
                  <code>{preview.baseUrl || "—"}</code>
                </dd>
              </div>
            </dl>
            <button type="button" className="link-btn" onClick={() => navigateHash("models")}>
              {zh ? "管理可用模型" : "Manage available models"}
            </button>
            <p className="muted text-label">
              {zh
                ? "应用前自动备份，断开时恢复。"
                : "Configuration is backed up before applying and restored on disconnect."}
            </p>
            <div className="codex-connection-actions">
              <button
                className="btn btn-primary"
                disabled={busy || home.trim() !== target || preview.state === "conflict"}
                onClick={() => void mutate("apply")}
              >
                {busy ? t("common.loading") : t("integrations.action.apply")}
              </button>
              <button
                className="btn btn-ghost"
                disabled={busy || !connected || home.trim() !== target}
                onClick={() => void mutate("sync")}
              >
                {zh ? "同步模型目录" : "Sync model catalog"}
              </button>
              {!target && (
                <button
                  className="btn btn-ghost"
                  disabled={busy || restart.restarting}
                  onClick={() => void restart.restart()}
                >
                  {t("dash.codexRestart")}
                </button>
              )}
              <button
                className="btn btn-ghost codex-connection-restore"
                disabled={
                  busy ||
                  (!connected && !preview.integration?.desiredEnabled) ||
                  home.trim() !== target
                }
                onClick={() => setRestore(true)}
              >
                {zh ? "断开并恢复" : "Disconnect & restore"}
              </button>
            </div>
          </div>
          {preview.integration?.message && (
            <Notice tone="warn">{preview.integration.message}</Notice>
          )}
          <details className="codex-connection-files">
            <summary>{zh ? "配置文件与目录" : "Configuration files and location"}</summary>
            <dl className="codex-connection-paths">
              <div>
                <dt>{zh ? "配置文件" : "Configuration"}</dt>
                <dd>
                  <code>{preview.configPath}</code>
                </dd>
              </div>
              {preview.catalogPath && (
                <div>
                  <dt>{zh ? "模型目录" : "Model catalog"}</dt>
                  <dd>
                    <code>{preview.catalogPath}</code>
                  </dd>
                </div>
              )}
            </dl>
            <form
              className="codex-connection-home"
              onSubmit={(event) => {
                event.preventDefault();
                setTarget(home.trim());
                setFeedback(null);
              }}
            >
              <label htmlFor="codex-home">
                {zh ? "自定义 Codex 配置目录" : "Custom Codex home"}
              </label>
              <div>
                <input
                  id="codex-home"
                  className="input"
                  value={home}
                  onChange={(event) => setHome(event.target.value)}
                  disabled={busy}
                  placeholder={zh ? "使用默认目录" : "Use default directory"}
                  aria-describedby="codex-home-hint"
                />
                <button type="submit" className="btn btn-ghost" disabled={busy}>
                  {zh ? "预览" : "Preview"}
                </button>
              </div>
              <p id="codex-home-hint" className="muted text-label">
                {zh
                  ? "留空使用 CODEX_HOME 或默认目录；修改后先预览，再应用。"
                  : "Leave blank to use CODEX_HOME or the default directory. Preview a changed location before applying."}
              </p>
            </form>
          </details>
        </>
      )}
      {feedback && <Notice tone={feedback.tone}>{feedback.text}</Notice>}
      {restore && preview && (
        <ConsequenceDialog
          copy={restoreCopy(preview.configPath)}
          onClose={() => setRestore(false)}
          onConfirm={() => mutate("restore")}
        />
      )}
    </section>
  );
}
