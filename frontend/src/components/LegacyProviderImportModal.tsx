import { useEffect, useId, useRef, useState } from "react";
import { useI18n } from "../i18n/shared";
import { liteTranslator } from "../i18n/lite-copy";

type ImportRow = { id: string; status: string; reason: string; adapter?: string };
type Preview = { source: string; digest: string; providers: ImportRow[]; notes: string[] };
type Result = { imported: string[]; backup: string | null; catalogRefreshPending: boolean };

export default function LegacyProviderImportModal({
  apiBase,
  onClose,
  onImported,
}: {
  apiBase: string;
  onClose: () => void;
  onImported: () => void;
}) {
  const { locale } = useI18n();
  const zh = locale === "zh" || locale === "zh-TW";
  const text = (en: string, cn: string) => (zh ? cn : en);
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  const [source, setSource] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement as HTMLElement | null;
    element?.showModal();
    return () => {
      element?.close();
      if (trigger?.isConnected) trigger.focus();
    };
  }, []);
  const request = async (apply: boolean) => {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `${apiBase}/api/provider-import/${apply ? "apply" : "preview"}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            apply ? { source: preview!.source, digest: preview!.digest, selected } : { source },
          ),
        },
      );
      if (!response.ok) throw new Error("import failed");
      if (apply) {
        setResult((await response.json()) as Result);
        onImported();
      } else {
        const value = (await response.json()) as Preview;
        setPreview(value);
        setSelected([]);
      }
    } catch {
      setError(
        text(
          "The operation could not complete. Check the source path and preview again before importing.",
          "操作未完成。请检查源路径，重新预览后再导入。",
        ),
      );
      if (apply) setPreview(null);
    } finally {
      setBusy(false);
    }
  };
  return (
    <dialog
      ref={dialog}
      className="modal-overlay"
      aria-labelledby={title}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <button
        type="button"
        className="modal-backdrop-dismiss"
        tabIndex={-1}
        aria-label={text("Close", "关闭")}
        onClick={() => {
          if (!busy) onClose();
        }}
      />
      <div
        className="modal-card"
        style={{ width: "min(720px, calc(100vw - 32px))", maxHeight: "85vh", overflowY: "auto" }}
      >
        <div className="modal-head">
          <h3 id={title}>
            {text("Import provider and model configuration", "导入供应商和模型配置")}
          </h3>
        </div>
        <div className="modal-body">
          <p>
            {text(
              "Import selected channels from original OpenCodex. Existing channels are retained; the source is read-only. OAuth login, history and global settings are separate.",
              "从原版 OpenCodex 导入选定渠道，保留现有渠道，源文件只读。OAuth 登录、历史记录和全局设置另行处理。",
            )}
          </p>
          {!result && (
            <>
              <label>
                {text("Original config.json or its directory", "原版 config.json 或所在目录")}
                <input
                  className="input"
                  value={source}
                  disabled={busy}
                  placeholder={liteTranslator(locale)("legacyImportPathPlaceholder")}
                  onChange={(event) => {
                    setSource(event.target.value);
                    setPreview(null);
                    setSelected([]);
                  }}
                />
              </label>
              <button
                type="button"
                className="btn"
                disabled={busy || !source.trim()}
                onClick={() => void request(false)}
              >
                {text("Preview", "预览")}
              </button>
              {preview && (
                <div>
                  {preview.providers.length === 0 && (
                    <p>{text("No providers found.", "未找到供应商。")}</p>
                  )}
                  {preview.providers.map((row) => (
                    <label key={row.id} style={{ display: "block", padding: "10px 0" }}>
                      <input
                        type="checkbox"
                        disabled={busy || !["ready", "disabled"].includes(row.status)}
                        checked={selected.includes(row.id)}
                        onChange={(event) =>
                          setSelected((current) =>
                            event.target.checked
                              ? [...current, row.id]
                              : current.filter((id) => id !== row.id),
                          )
                        }
                      />{" "}
                      <strong>{row.id}</strong>
                      {" · "}
                      {row.adapter || row.status}
                      {row.reason && <div className="muted">{row.reason}</div>}
                      {row.status === "disabled" && (
                        <div className="muted">
                          {text(
                            "Imported disabled. Review its settings and credentials before enabling it.",
                            "导入后保持停用，检查设置和凭据后可再启用。",
                          )}
                        </div>
                      )}
                    </label>
                  ))}
                </div>
              )}
            </>
          )}
          {result && (
            <div role="status">
              <p>
                {text("Imported", "已导入")}: {result.imported.join(", ")}
              </p>
              {result.backup && (
                <p>
                  {text("Previous Lite configuration backup", "Lite 原配置备份")}:{" "}
                  <code style={{ overflowWrap: "anywhere" }}>{result.backup}</code>
                </p>
              )}
              {result.catalogRefreshPending && (
                <p>
                  {text(
                    "Configuration saved. Model catalog refresh is pending; refresh it from Providers when the connection is available.",
                    "配置已保存，模型目录刷新尚未完成；连接恢复后可在供应商页面刷新。",
                  )}
                </p>
              )}
            </div>
          )}
          {error && <p role="alert">{error}</p>}
        </div>
        <div className="modal-foot row">
          <button type="button" className="btn" disabled={busy} onClick={onClose}>
            {text("Close", "关闭")}
          </button>
          {!result && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || !preview || selected.length === 0}
              onClick={() => void request(true)}
            >
              {busy ? text("Working…", "处理中…") : text("Import selected", "导入所选渠道")}
            </button>
          )}
        </div>
      </div>
    </dialog>
  );
}
