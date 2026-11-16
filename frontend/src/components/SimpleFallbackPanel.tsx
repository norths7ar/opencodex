import { Panel, PanelBody } from "@/components/settings/panel";
import { useCallback, useState } from "react";
import { useDataSurface } from "../data-surface";
import { readJsonOrThrow } from "../fetch-json";
import { useI18n } from "../i18n/shared";
import { Notice, Select } from "../ui";
import { Button } from "./button";

interface FallbackEvent {
  at: string;
  from: string;
  to: string;
  trigger: string;
  outcome: string;
  reason?: string;
}
interface FallbackState {
  enabled: boolean;
  routes: Record<string, string[]>;
  last: FallbackEvent | null;
}
interface ModelRow {
  provider: string;
  id: string;
}
interface RouteDraft {
  key: string;
  source: string;
  targets: string[];
}

export default function SimpleFallbackPanel({
  apiBase,
  readOnly = false,
}: {
  apiBase: string;
  readOnly?: boolean;
}) {
  const { t } = useI18n();
  const load = useCallback(
    async (signal: AbortSignal) => {
      const [policy, models] = await Promise.all([
        fetch(`${apiBase}/api/simple-fallback`, { signal }).then((response) =>
          readJsonOrThrow<FallbackState>(response, "Could not load fallback settings."),
        ),
        fetch(`${apiBase}/api/models`, { signal }).then((response) =>
          readJsonOrThrow<ModelRow[]>(response, "Could not load models."),
        ),
      ]);
      if (!policy || !Array.isArray(models))
        throw new Error("Incomplete fallback settings response.");
      return { policy, models };
    },
    [apiBase],
  );
  const resource = useDataSurface(`simple-fallback:${apiBase}`, [apiBase], load, {
    isEmpty: () => false,
  });
  const data = resource.state.data;
  return (
    <Panel
      id="simple-fallback"
      title={t("fallback.surface.fallbackModels")}
      description={t("fallback.surface.description")}
    >
      <PanelBody>
        <div className="flex flex-col gap-4 p-4">
          {resource.state.showError && (
            <Notice tone="err">{t("fallback.surface.couldNotLoadFallbackSettings")}</Notice>
          )}
          {!data && !resource.state.showError && (
            <p role="status">{t("fallback.surface.loading")}</p>
          )}
          {data && (
            <FallbackEditor
              key={`${apiBase}:${JSON.stringify(data.policy)}`}
              apiBase={apiBase}
              state={data.policy}
              models={data.models}
              readOnly={readOnly}
              onSaved={() => resource.refresh()}
            />
          )}
          <div>
            <Button
              size="sm"
              disabled={resource.state.refreshing}
              onClick={() => resource.refresh()}
            >
              {t("fallback.surface.refresh")}
            </Button>
          </div>
        </div>
      </PanelBody>
    </Panel>
  );
}

function FallbackEditor({
  apiBase,
  state,
  models,
  readOnly,
  onSaved,
}: {
  apiBase: string;
  state: FallbackState;
  models: ModelRow[];
  readOnly: boolean;
  onSaved: () => void;
}) {
  const { t } = useI18n();
  const [enabled, setEnabled] = useState(state.enabled);
  const [routes, setRoutes] = useState<RouteDraft[]>(() =>
    Object.entries(state.routes).map(([source, targets]) => ({
      key: crypto.randomUUID(),
      source,
      targets,
    })),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const blocked = readOnly || saving;
  const selectors = [...new Set(models.map((row) => `${row.provider}/${row.id}`))];
  const options = (current: string) => [
    { value: "", label: t("fallback.surface.selectModel") },
    ...[...new Set([...selectors, ...(current ? [current] : [])])].map((value) => ({
      value,
      label: value,
    })),
  ];
  const valid =
    new Set(routes.map((route) => route.source)).size === routes.length &&
    routes.every(
      (route) =>
        route.source &&
        route.targets.length > 0 &&
        route.targets.every((target) => target && target !== route.source) &&
        new Set(route.targets).size === route.targets.length,
    );
  const update = (key: string, patch: Partial<RouteDraft>) => {
    setSaved(false);
    setRoutes((current) =>
      current.map((route) => (route.key === key ? { ...route, ...patch } : route)),
    );
  };
  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const response = await fetch(`${apiBase}/api/simple-fallback`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled,
          routes: Object.fromEntries(routes.map((route) => [route.source, route.targets])),
        }),
      });
      await readJsonOrThrow(response, t("fallback.surface.saveFailed"));
      setSaved(true);
      onSaved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSaving(false);
    }
  };
  return (
    <>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={enabled}
          disabled={blocked}
          onChange={(event) => {
            setEnabled(event.target.checked);
            setSaved(false);
          }}
        />
        {t("fallback.surface.enableFallbackRoutes")}
      </label>
      {routes.map((route) => (
        <fieldset
          key={route.key}
          className="flex min-w-0 flex-col gap-3 rounded-control border border-line p-3"
          disabled={blocked}
        >
          <legend className="text-sm font-medium">
            {t("fallback.surface.primaryModelAndBackupOrder")}
          </legend>
          <Select
            value={route.source}
            options={options(route.source)}
            onChange={(source) => update(route.key, { source })}
            label={t("fallback.surface.primaryModel")}
            disabled={blocked}
            fullWidth
          />
          {route.targets.map((target, index) => (
            <div key={index} className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="text-sm text-fg-muted">{index + 1}.</span>
              <div className="min-w-0 flex-1">
                <Select
                  value={target}
                  options={options(target)}
                  onChange={(value) =>
                    update(route.key, {
                      targets: route.targets.map((item, position) =>
                        position === index ? value : item,
                      ),
                    })
                  }
                  label={`${t("fallback.surface.backupModel")} ${index + 1}`}
                  disabled={blocked}
                  fullWidth
                />
              </div>
              <Button
                size="sm"
                disabled={blocked || index === 0}
                onClick={() => {
                  const targets = [...route.targets];
                  [targets[index - 1], targets[index]] = [targets[index]!, targets[index - 1]!];
                  update(route.key, { targets });
                }}
              >
                {t("fallback.surface.moveUp")}
              </Button>
              <Button
                size="sm"
                disabled={blocked}
                onClick={() =>
                  update(route.key, {
                    targets: route.targets.filter((_, position) => position !== index),
                  })
                }
              >
                {t("fallback.surface.remove")}
              </Button>
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={blocked}
              onClick={() => update(route.key, { targets: [...route.targets, ""] })}
            >
              {t("fallback.surface.addBackup")}
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={blocked}
              onClick={() => {
                setRoutes((current) => current.filter((item) => item.key !== route.key));
                setSaved(false);
              }}
            >
              {t("fallback.surface.deleteRoute")}
            </Button>
          </div>
        </fieldset>
      ))}
      {!valid && <p className="text-sm text-fg-muted">{t("fallback.surface.invalidRoutes")}</p>}
      <div className="flex flex-wrap gap-2">
        <Button
          disabled={blocked}
          onClick={() => {
            setRoutes((current) => [
              ...current,
              { key: crypto.randomUUID(), source: "", targets: [""] },
            ]);
            setSaved(false);
          }}
        >
          {t("fallback.surface.addRoute")}
        </Button>
        <Button variant="primary" disabled={blocked || !valid} onClick={() => void save()}>
          {saving ? t("fallback.surface.saving") : t("fallback.surface.save")}
        </Button>
      </div>
      {error && <Notice tone="err">{error}</Notice>}
      {saved && <Notice tone="ok">{t("fallback.surface.saved")}</Notice>}
      <p className="m-0 text-sm text-fg-muted">{t("fallback.surface.deliveryBoundary")}</p>
      {state.last && (
        <div className="text-sm">
          <strong>{t("fallback.surface.lastFallback")}</strong>
          <p className="break-all">
            {state.last.from} → {state.last.to} · {state.last.trigger} · {state.last.outcome}
            {state.last.reason ? ` · ${state.last.reason}` : ""}
          </p>
        </div>
      )}
    </>
  );
}
