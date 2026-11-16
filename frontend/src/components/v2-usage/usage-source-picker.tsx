import { LaptopIcon } from "@hugeicons/core-free-icons";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
  type SetStateAction,
} from "react";
import {
  USAGE_SESSION_CHANGED,
  USAGE_SOURCE_INVENTORY_CHANGED,
  denyUsageSession,
  sourceInventoryEventMatches,
  usageSessionSnapshot,
} from "../../api";
import { withFetchDeadline } from "../../bounded-fetch";
import { clientIconClass } from "../../client-icon";
import type { Locale, TFn } from "../../i18n/shared";
import { IconCheck, IconGlobe, IconKey, IconMonitor } from "../../icons";
import { cn } from "../../lib/utils";
import { useRuntimeCapabilities } from "../../runtime-capabilities";
import { Select } from "../../ui";
import {
  SOURCE_CLIENTS,
  availableSourceClients,
  matchesFacetWindow,
  matchesSource,
  noStore,
  sourceQuery,
  type SourceClient,
  type SourceDevice,
  type SourceFilter,
} from "./usage-source";

type Facets = {
  sourceFilter: unknown;
  window: { since: number; until: number; timeZone: string };
  apps: Record<SourceClient, number>;
  devices: { device: SourceDevice; requests: number }[];
  legacyUnclassifiedRequests: number;
  usageIncomplete: boolean;
  unavailableConfiguredKeys?: number;
};
const clientNames: Record<SourceClient, string> = {
  all: "",
  codex: "Codex",
  dsh: "DSH",
  claude_code: "Claude Code",
  claude_desktop: "Claude Desktop",
  grok: "Grok",
  opencode: "OpenCode",
  aside: "Aside",
  other: "",
  unknown: "",
};
const sameDevice = (a: SourceDevice, b: SourceDevice) =>
  a.kind === b.kind && (a.kind !== "key" || (b.kind === "key" && a.id === b.id));
const validCount = (n: unknown) => typeof n === "number" && Number.isSafeInteger(n) && n >= 0;
const defaultDevices: SourceDevice[] = [
  { kind: "all" },
  { kind: "no-key" },
  { kind: "unconfigured" },
];
const clientMarks: Partial<Record<SourceClient, string>> = {
  codex: "openai.svg",
  dsh: "deepseek-harness.svg",
  claude_code: "claude-color.svg",
  claude_desktop: "claude-color.svg",
  grok: "grok.svg",
  opencode: "opencode.svg",
  aside: "aside.svg",
};
function appMark(client: SourceClient) {
  const mark = clientMarks[client];
  return mark ? (
    <img className={clientIconClass(mark)} src={`/provider-icons/${mark}`} alt="" />
  ) : (
    <IconGlobe width={16} height={16} aria-hidden="true" />
  );
}
function onSourceKeyDown(event: KeyboardEvent<HTMLDivElement>) {
  if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const active = event.target as HTMLButtonElement;
  if (active.getAttribute("role") !== "radio") return;
  const group = active.closest('[role="radiogroup"]');
  const radios = [...(group?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? [])];
  const index = radios.indexOf(active);
  if (index < 0 || !radios.length) return;
  event.preventDefault();
  const next =
    radios[
      (index + (event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1) + radios.length) %
        radios.length
    ];
  next?.focus();
  next?.click();
}
function validFacets(value: Facets, filter: SourceFilter): boolean {
  if (
    !value ||
    !matchesSource(value.sourceFilter, filter) ||
    !value.window ||
    !Number.isFinite(value.window.since) ||
    !matchesFacetWindow(value.window) ||
    typeof value.usageIncomplete !== "boolean" ||
    !value.apps ||
    !SOURCE_CLIENTS.every((client) => validCount(value.apps[client])) ||
    !validCount(value.legacyUnclassifiedRequests) ||
    (value.unavailableConfiguredKeys !== undefined &&
      (!validCount(value.unavailableConfiguredKeys) || value.unavailableConfiguredKeys > 1024)) ||
    !Array.isArray(value.devices)
  )
    return false;
  if (value.devices.length > 1027) return false;
  const seen = new Set<string>();
  const keyIds = new Set<string>();
  let total = 0;
  for (const row of value.devices) {
    if (!row || !validCount(row.requests) || !row.device || typeof row.device !== "object")
      return false;
    const device = row.device;
    if (device.kind === "key") {
      if (
        typeof device.id !== "string" ||
        !device.id ||
        device.id !== device.id.trim() ||
        device.id.length > 256 ||
        (total += device.id.length) > 65536 ||
        Object.keys(device).sort().join(",") !== "id,kind"
      )
        return false;
      if (keyIds.has(device.id)) return false;
      keyIds.add(device.id);
    } else {
      if (
        !["all", "no-key", "unconfigured"].includes(device.kind) ||
        Object.keys(device).join(",") !== "kind" ||
        seen.has(device.kind)
      )
        return false;
      seen.add(device.kind);
    }
  }
  return ["all", "no-key", "unconfigured"].every((kind) => seen.has(kind));
}

type FacetResult = { key: string; facets?: Facets; failed?: boolean };
type RosterState = { identity: string; devices: SourceDevice[]; clients: SourceClient[] };
type ReportApp = { client: string; requests: number };
const reportClients = (apps: readonly ReportApp[] | undefined) =>
  SOURCE_CLIENTS.filter((client) => apps?.some((app) => app.client === client && app.requests > 0));

// Keep the refresh lifecycle together: every request, timer, and event listener
// belongs to this one subscription and is cancelled when it is disposed.
function subscribeToFacets({
  apiBase,
  requested,
  generation,
  identity,
  resultKey,
  setResult,
  setRoster,
}: {
  apiBase: string;
  requested: SourceFilter;
  generation: number;
  identity: string;
  resultKey: string;
  setResult: Dispatch<SetStateAction<FacetResult>>;
  setRoster: Dispatch<SetStateAction<RosterState>>;
}) {
  let disposed = false;
  let sequence = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  const load = async () => {
    const id = ++sequence;
    controller?.abort();
    controller = new AbortController();
    const currentController = controller;
    setResult((previous) => (previous.key === resultKey ? previous : { key: resultKey }));
    try {
      const facets = await withFetchDeadline(60_000, currentController.signal, async (signal) => {
        const response = await fetch(
          apiBase + "/api/usage/source-facets?" + sourceQuery(requested),
          { signal, cache: "no-store" },
        );
        if (response.status === 401 && !signal.aborted && id === sequence)
          denyUsageSession(generation);
        if (!response.ok || !noStore(response)) throw new Error("usage source unavailable");
        const value = (await response.json()) as Facets;
        if (!validFacets(value, requested)) throw new Error("invalid facets");
        return value;
      });
      if (!disposed && id === sequence && usageSessionSnapshot().generation === generation) {
        setRoster((previous) => ({
          identity,
          devices: facets.devices.map((row) => row.device),
          clients: [
            ...new Set([
              ...(previous.identity === identity ? previous.clients : []),
              ...SOURCE_CLIENTS.filter((client) => facets.apps[client] > 0),
            ]),
          ],
        }));
        setResult({ key: resultKey, facets });
      }
    } catch {
      currentController.abort();
      if (!disposed && id === sequence)
        setResult((previous) => ({
          ...(previous.key === resultKey ? previous : { key: resultKey }),
          failed: true,
        }));
    } finally {
      if (!disposed && id === sequence) timer = setTimeout(load, 15_000);
    }
  };
  void load();
  const focus = () => {
    if (timer) clearTimeout(timer);
    void load();
  };
  const inventoryChanged = (event: Event) => {
    if (sourceInventoryEventMatches(event, apiBase)) focus();
  };
  window.addEventListener("focus", focus);
  window.addEventListener(USAGE_SOURCE_INVENTORY_CHANGED, inventoryChanged);
  return () => {
    disposed = true;
    sequence++;
    controller?.abort();
    if (timer) clearTimeout(timer);
    window.removeEventListener("focus", focus);
    window.removeEventListener(USAGE_SOURCE_INVENTORY_CHANGED, inventoryChanged);
  };
}

export function UsageSourcePicker({
  apiBase,
  filter,
  onClient,
  onDevice,
  deviceLocked,
  facetsAllowed,
  reportApps,
  session,
  t,
  locale,
}: {
  apiBase: string;
  filter: SourceFilter | null;
  onClient: (client: SourceClient) => void;
  onDevice: (device: SourceDevice) => void;
  deviceLocked: boolean;
  facetsAllowed: boolean;
  reportApps?: readonly ReportApp[];
  session: ReturnType<typeof usageSessionSnapshot>;
  t: TFn;
  locale: Locale;
}) {
  const capabilities = useRuntimeCapabilities();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [result, setResult] = useState<{ key: string; facets?: Facets; failed?: boolean }>({
    key: "",
  });
  const [roster, setRoster] = useState<RosterState>({ identity: "", devices: [], clients: [] });
  const identity = JSON.stringify([apiBase, session.generation]);
  const [reportInventory, setReportInventory] = useState({
    identity,
    reportApps,
    clients: reportClients(reportApps),
  });
  if (reportInventory.identity !== identity || reportInventory.reportApps !== reportApps) {
    setReportInventory({
      identity,
      reportApps,
      clients: [
        ...new Set([
          ...(reportInventory.identity === identity ? reportInventory.clients : []),
          ...reportClients(reportApps),
        ]),
      ],
    });
  }
  const input = useRef<HTMLInputElement>(null);
  const filterKey = JSON.stringify(filter);
  const clientLabel = (client: SourceClient) =>
    clientNames[client] ||
    t(
      client === "all"
        ? "usageV2.allApps"
        : client === "other"
          ? "usageV2.otherApps"
          : "usageV2.unknown",
    );
  const resultKey = JSON.stringify([filterKey, identity]);
  const data = !session.blocked && result.key === resultKey ? result : { key: resultKey };
  const deviceLabel = (device: SourceDevice) => {
    if (device.kind === "all") return t("usageV2.allDevices");
    if (device.kind === "no-key") return t("usageV2.thisDevice");
    if (device.kind === "unconfigured") return t("usageV2.unconfigured");
    if (device.kind === "key") {
      const rows =
        !session.blocked && roster.identity === identity ? roster.devices : defaultDevices;
      const index = rows
        .filter((row) => row.kind === "key")
        .findIndex((row) => sameDevice(row, device));
      return t("usageV2.unknownDeviceNumber", { number: Math.max(1, index + 1) });
    }
    return t("usageV2.unknown");
  };
  useEffect(() => {
    const requested = JSON.parse(filterKey) as SourceFilter | null;
    if (!open || !requested || !facetsAllowed || session.blocked) return;
    return subscribeToFacets({
      apiBase,
      requested,
      generation: session.generation,
      identity,
      resultKey,
      setResult,
      setRoster,
    });
  }, [
    open,
    apiBase,
    facetsAllowed,
    filterKey,
    identity,
    resultKey,
    session.blocked,
    session.generation,
  ]);
  useEffect(() => {
    const clear = () => {
      setRoster({ identity: "", devices: [], clients: [] });
      setResult({ key: "" });
    };
    const changed = (event: Event) => {
      if (sourceInventoryEventMatches(event, apiBase)) clear();
    };
    window.addEventListener(USAGE_SOURCE_INVENTORY_CHANGED, changed);
    window.addEventListener(USAGE_SESSION_CHANGED, clear);
    return () => {
      window.removeEventListener(USAGE_SOURCE_INVENTORY_CHANGED, changed);
      window.removeEventListener(USAGE_SESSION_CHANGED, clear);
    };
  }, [apiBase]);
  const appDetail = (client: SourceClient) =>
    client === "codex" && (data.facets?.legacyUnclassifiedRequests ?? 0) > 0
      ? t("usageV2.codexUnclassified", {
          count: data.facets!.legacyUnclassifiedRequests.toLocaleString(locale),
        })
      : "";
  const q = search.trim().toLocaleLowerCase(locale);
  const visible = (text: string) => !q || text.toLocaleLowerCase(locale).includes(q);
  const count = (value: number | undefined) =>
    data.facets && !data.facets.usageIncomplete && value !== undefined
      ? value.toLocaleString(locale)
      : "—";
  const available =
    !session.blocked && roster.identity === identity ? roster.devices : defaultDevices;
  const selectedDevice = filter?.device;
  const devices: SourceDevice[] = deviceLocked
    ? filter
      ? [filter.device]
      : []
    : selectedDevice && !available.some((item) => sameDevice(item, selectedDevice))
      ? [...available, selectedDevice]
      : available;
  const visibleClients = availableSourceClients(
    capabilities?.clients,
    data.facets?.apps,
    filter?.client,
    session.blocked
      ? []
      : [
          ...(roster.identity === identity ? roster.clients : []),
          ...(reportInventory.identity === identity ? reportInventory.clients : []),
        ],
  ).filter((client) => visible(`${clientLabel(client)} ${appDetail(client)}`));
  const visibleDevices = devices.filter(
    (device) =>
      visible(deviceLabel(device)) &&
      (device.kind === "all" ||
        device.kind === "no-key" ||
        (filter && sameDevice(device, filter.device)) ||
        (data.facets?.devices.find((row) => sameDevice(row.device, device))?.requests ?? 0) > 0),
  );
  const selectedClientVisible = !!filter && visibleClients.includes(filter.client);
  const selectedDeviceVisible =
    !!filter && visibleDevices.some((item) => sameDevice(filter.device, item));
  return (
    <Select.Popup
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setSearch("");
      }}
      label={t("usageV2.source")}
      initialFocusRef={input}
      maxHeight={700}
      className="ui-v2 overflow-auto rounded-panel bg-raised p-3 text-[13px] text-fg shadow-lg ring-1 ring-line"
      onKeyDown={onSourceKeyDown}
      trigger={
        <>
          {appMark(filter?.client ?? "all")}
          <span>{clientLabel(filter?.client ?? "all")}</span>
          <span aria-hidden="true">·</span>
          <span>{filter ? deviceLabel(filter.device) : t("usageV2.unknown")}</span>
        </>
      }
    >
      <input
        ref={input}
        className="input w-full"
        type="search"
        aria-label={t("usageV2.searchSources")}
        placeholder={t("usageV2.searchSources")}
        value={search}
        onChange={(event) => setSearch(event.currentTarget.value)}
      />
      <div role="radiogroup" aria-label={t("usageV2.apps")} className="mt-3">
        <div className="flex justify-between px-2 text-fg-muted">
          <span>{t("usageV2.apps")}</span>
          <span>{t("usageV2.last30")}</span>
        </div>
        {visibleClients.map((client) => {
          const selected = filter?.client === client;
          return (
            <button
              key={client}
              type="button"
              role="radio"
              aria-checked={selected}
              tabIndex={
                selected || (!selectedClientVisible && visibleClients[0] === client) ? 0 : -1
              }
              onClick={() => onClient(client)}
              className={cn(
                "flex w-full items-center gap-3 rounded-control px-2 py-1.5 text-left hover:bg-surface focus-visible:outline-2 focus-visible:outline-highlight",
                selected && "bg-surface",
              )}
            >
              <span className="shrink-0 text-fg-muted">{appMark(client)}</span>
              <span className="min-w-0 flex-1">
                <span className="block">{clientLabel(client)}</span>
                {appDetail(client) && (
                  <span className="block text-xs text-fg-muted">{appDetail(client)}</span>
                )}
              </span>
              <span className="text-fg-muted tabular-nums">{count(data.facets?.apps[client])}</span>
              {selected && <IconCheck width={16} height={16} aria-hidden="true" />}
            </button>
          );
        })}
      </div>
      <div
        role="radiogroup"
        aria-label={t("usageV2.devices")}
        className="mt-3 border-t border-line pt-3"
      >
        <div className="flex justify-between px-2 text-fg-muted">
          <span>{t("usageV2.devices")}</span>
          <span>{t("usageV2.last30")}</span>
        </div>
        {visibleDevices.map((device) => {
          const selected = !!filter && sameDevice(filter.device, device);
          return (
            <button
              key={device.kind === "key" ? `key:${device.id}` : device.kind}
              type="button"
              role="radio"
              aria-checked={selected}
              tabIndex={
                filter && (selected || (!selectedDeviceVisible && visibleDevices[0] === device))
                  ? 0
                  : -1
              }
              onClick={() => onDevice(device)}
              disabled={deviceLocked}
              className={cn(
                "flex w-full items-center gap-3 rounded-control px-2 py-1.5 text-left hover:bg-surface focus-visible:outline-2 focus-visible:outline-highlight",
                selected && "bg-surface",
              )}
            >
              <span className="shrink-0 text-fg-muted">
                {device.kind === "key" ? (
                  <HugeiconsIcon icon={LaptopIcon} size={16} aria-hidden="true" />
                ) : device.kind === "unconfigured" ? (
                  <IconKey width={16} height={16} aria-hidden="true" />
                ) : (
                  <IconMonitor width={16} height={16} aria-hidden="true" />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block">{deviceLabel(device)}</span>
              </span>
              <span className="text-fg-muted tabular-nums">
                {count(
                  data.facets?.devices.find((row) => sameDevice(row.device, device))?.requests,
                )}
              </span>
              {selected && <IconCheck width={16} height={16} aria-hidden="true" />}
            </button>
          );
        })}
      </div>
      {deviceLocked && <p className="mb-0 mt-3 text-fg-muted">{t("usageV2.locked")}</p>}
      {!!data.facets?.unavailableConfiguredKeys && (
        <p role="status" className="mb-0 text-fg-muted">
          {t("usageV2.unavailableKeys", {
            count: data.facets.unavailableConfiguredKeys.toLocaleString(locale),
          })}
        </p>
      )}
      {(data.failed || data.facets?.usageIncomplete || !facetsAllowed) && (
        <p role="status" className="mb-0 text-fg-muted">
          {t(data.failed && data.facets ? "usage.loadError" : "usageV2.countsUnavailable")}
        </p>
      )}
    </Select.Popup>
  );
}
