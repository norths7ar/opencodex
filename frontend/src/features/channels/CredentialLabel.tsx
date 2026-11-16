import { Button } from "@/components/button";
import { readJsonOrThrow } from "@/fetch-json";
import { Notice } from "@/ui";
import { useState } from "react";
import { useChannelCopy } from "./copy";
export function CredentialLabel({
  apiBase,
  channelId,
  label,
  onSaved,
}: {
  apiBase: string;
  channelId: string;
  label?: string | null;
  onSaved: () => void;
}) {
  const c = useChannelCopy();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(label ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await fetch(`${apiBase}/api/channels/${encodeURIComponent(channelId)}/credential-label`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: draft.trim() || null }),
      }).then(readJsonOrThrow);
      setEditing(false);
      onSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : c("error"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="channel-credential">
      <div className="channel-credential-row">
        <span className="channel-credential-title">{c("credentialsLabel")}</span>
        {editing ? (
          <div className="channel-actions">
            <input
              className="input"
              aria-label={c("credentialsLabel")}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={busy}
            />
            <Button size="sm" disabled={busy} onClick={() => void save()}>
              {c("save")}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => setEditing(false)}>
              {c("cancel")}
            </Button>
          </div>
        ) : (
          <div className="channel-actions">
            <span>{label || "—"}</span>
            <Button
              size="sm"
              onClick={() => {
                setDraft(label ?? "");
                setEditing(true);
              }}
            >
              {c("edit")}
            </Button>
          </div>
        )}
      </div>
      {error && <Notice tone="err">{error}</Notice>}
    </div>
  );
}
