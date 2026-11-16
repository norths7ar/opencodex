import { useState } from "react";
import { Button } from "../../components/button";
import { useClientCopy } from "./client-copy";
import type { IntegrationJournalRow } from "./integration-api";

export function RollbackHistory({
  rows,
  onRestore,
  onDelete,
}: {
  rows: readonly IntegrationJournalRow[];
  onRestore: (row: IntegrationJournalRow) => void;
  onDelete: (row: IntegrationJournalRow) => void;
}) {
  const copy = useClientCopy();
  const [shown, setShown] = useState(6);
  const rowView = (row: IntegrationJournalRow) => (
    <li key={row.opId} className="client-history-row">
      <div>
        <span>
          {copy(
            row.kind === "apply"
              ? "applied"
              : row.kind === "disable"
                ? "disabled"
                : row.kind === "overwrite"
                  ? "replaced"
                  : row.kind === "refresh"
                    ? "refreshed"
                    : "restored",
          )}
        </span>
        <time dateTime={row.at}>{new Date(row.at).toLocaleString()}</time>
        <code>{row.configPath}</code>
      </div>
      <div className="client-actions">
        {row.snapshot === "expired" ? (
          <span className="muted">{copy("expired")}</span>
        ) : (
          <Button size="sm" onClick={() => onRestore(row)}>
            {copy("restore")}
          </Button>
        )}
        {row.deletable && (
          <Button size="sm" variant="ghost" onClick={() => onDelete(row)}>
            {copy("removeEntry")}
          </Button>
        )}
      </div>
    </li>
  );
  return (
    <section className="client-history">
      <h3>{copy("history")}</h3>
      {rows.length === 0 ? (
        <p className="muted">{copy("noHistory")}</p>
      ) : (
        <>
          <ul>{rowView(rows[0])}</ul>
          {rows.length > 1 && (
            <details>
              <summary>{copy("older")}</summary>
              <ul>{rows.slice(1, shown + 1).map(rowView)}</ul>
              {rows.length > shown + 1 && (
                <Button size="sm" onClick={() => setShown(shown + 6)}>
                  {copy("more")}
                </Button>
              )}
            </details>
          )}
        </>
      )}
    </section>
  );
}
