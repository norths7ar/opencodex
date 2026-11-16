import { Button } from "../../components/button";
import { DataSurfaceSkeleton } from "../../components/data-surface";
import { navigateHash } from "../../hash-routing";
import { Notice } from "../../ui";
import { useClientCopy } from "./client-copy";
import { useCodexConnection, useDshConnection } from "./client-resources";
import IntegrationStateBadge from "./IntegrationStateBadge";
import type { IntegrationState } from "./integration-api";
import type { ResourceSnapshot } from "../../client-resource";
import type { ClientApplicationOutcome } from "../../contracts/channels";
import { useT } from "../../i18n/shared";
import ClientMark from "../../components/ClientMark";
import { markFor } from "../../components/integration-marks";

function ClientCard({
  name,
  client,
  description,
  path,
  state,
  resource,
  route,
  lastCatalogApplication,
}: {
  name: string;
  client: "codex" | "dsh";
  description: string;
  path?: string;
  state?: IntegrationState;
  resource: ResourceSnapshot<unknown> & { refresh: () => void };
  route: string;
  lastCatalogApplication?: ClientApplicationOutcome | null;
}) {
  const copy = useClientCopy();
  const t = useT();
  return (
    <article className="client-card">
      <header className="client-section-heading">
        <ClientMark src={markFor(client)} label={name} size={28} />
        <h3>{name}</h3>
        <IntegrationStateBadge state={state ?? (resource.error ? "unavailable" : "unknown")} />
      </header>
      <p className="muted">{description}</p>
      {!resource.data && !resource.error ? (
        <DataSurfaceSkeleton label={copy("loading")} rows={2} />
      ) : (
        <div className="client-card-detail">{path && <code>{path}</code>}</div>
      )}
      {Boolean(resource.error) && <Notice tone="err">{String(resource.error)}</Notice>}
      {lastCatalogApplication?.ok === false && (
        <Notice tone="warn">
          {t("integrations.lastCatalogSyncFailed")}{" "}
          {lastCatalogApplication.message || lastCatalogApplication.reason}
        </Notice>
      )}
      <div className="client-actions">
        <Button variant="primary" onClick={() => navigateHash(route)}>
          {copy("configure")}
        </Button>
        <Button variant="ghost" disabled={resource.refreshing} onClick={() => resource.refresh()}>
          {copy("refresh")}
        </Button>
      </div>
    </article>
  );
}
export default function IntegrationsOverview({
  apiBase,
  active,
}: {
  apiBase: string;
  active: boolean;
}) {
  const copy = useClientCopy();
  const codex = useCodexConnection(apiBase, "", active);
  const dsh = useDshConnection(apiBase, active);
  return (
    <div className="client-grid">
      <ClientCard
        client="codex"
        name={copy("codex")}
        description={copy("codexPurpose")}
        path={codex.data?.configPath}
        state={codex.data?.integration.state}
        resource={codex}
        route="integrations/codex"
        lastCatalogApplication={codex.data?.lastCatalogApplication}
      />
      <ClientCard
        client="dsh"
        name={copy("dsh")}
        description={copy("dshPurpose")}
        path={dsh.data?.configPath}
        state={dsh.data?.state}
        resource={dsh}
        route="integrations/dsh"
        lastCatalogApplication={dsh.data?.lastCatalogApplication}
      />
    </div>
  );
}
