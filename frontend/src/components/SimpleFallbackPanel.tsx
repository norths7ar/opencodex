import { useCallback, useState } from "react";
import { useI18n } from "../i18n/shared";
import { useDataSurface } from "../data-surface";
import { readJsonOrThrow } from "../fetch-json";
import { Select, Notice } from "../ui";
import { Button } from "./ui-v2/button";
import { SettingsSection } from "./v2-proxy/settings-layout";

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
  const { locale } = useI18n();
  const zh = locale === "zh" || locale === "zh-TW";
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
    <SettingsSection
      id="simple-fallback"
      title={zh ? "备用模型" : "Fallback models"}
      lede={
        zh
          ? "主目标连接失败，或因账号、额度、模型可用性及服务错误无法完成请求时，按指定顺序尝试备用模型。默认关闭。"
          : "Try selected backups in order when connection, account, quota, model availability, or service errors prevent the primary from serving a request. Off by default."
      }
    >
      <div className="flex flex-col gap-4 p-4">
        {resource.state.showError && (
          <Notice tone="err">
            {zh ? "无法加载备用模型设置。" : "Could not load fallback settings."}
          </Notice>
        )}
        {!data && !resource.state.showError && <p role="status">{zh ? "正在加载…" : "Loading…"}</p>}
        {data && (
          <FallbackEditor
            key={`${apiBase}:${JSON.stringify(data.policy)}`}
            apiBase={apiBase}
            state={data.policy}
            models={data.models}
            zh={zh}
            readOnly={readOnly}
            onSaved={() => resource.refresh()}
          />
        )}
        <div>
          <Button size="sm" disabled={resource.state.refreshing} onClick={() => resource.refresh()}>
            {zh ? "刷新" : "Refresh"}
          </Button>
        </div>
      </div>
    </SettingsSection>
  );
}

function FallbackEditor({
  apiBase,
  state,
  models,
  zh,
  readOnly,
  onSaved,
}: {
  apiBase: string;
  state: FallbackState;
  models: ModelRow[];
  zh: boolean;
  readOnly: boolean;
  onSaved: () => void;
}) {
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
    { value: "", label: zh ? "选择模型" : "Select model" },
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
      await readJsonOrThrow(response, zh ? "保存失败。" : "Save failed.");
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
        {zh ? "启用备用路由" : "Enable fallback routes"}
      </label>
      {routes.map((route) => (
        <fieldset
          key={route.key}
          className="flex min-w-0 flex-col gap-3 rounded-control border border-line p-3"
          disabled={blocked}
        >
          <legend className="text-sm font-medium">
            {zh ? "主模型及其备用顺序" : "Primary model and backup order"}
          </legend>
          <Select
            value={route.source}
            options={options(route.source)}
            onChange={(source) => update(route.key, { source })}
            label={zh ? "主模型" : "Primary model"}
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
                  label={`${zh ? "备用模型" : "Backup model"} ${index + 1}`}
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
                {zh ? "上移" : "Move up"}
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
                {zh ? "移除" : "Remove"}
              </Button>
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={blocked}
              onClick={() => update(route.key, { targets: [...route.targets, ""] })}
            >
              {zh ? "添加备用模型" : "Add backup"}
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
              {zh ? "删除路由" : "Delete route"}
            </Button>
          </div>
        </fieldset>
      ))}
      {!valid && (
        <p className="text-sm text-fg-muted">
          {zh
            ? "每条路由需要一个主模型和至少一个不同的备用模型，模型不能重复。"
            : "Each route needs a primary and at least one distinct backup; duplicate models are not allowed."}
        </p>
      )}
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
          {zh ? "添加路由" : "Add route"}
        </Button>
        <Button variant="primary" disabled={blocked || !valid} onClick={() => void save()}>
          {saving ? (zh ? "正在保存…" : "Saving…") : zh ? "保存" : "Save"}
        </Button>
      </div>
      {error && <Notice tone="err">{error}</Notice>}
      {saved && <Notice tone="ok">{zh ? "已保存。" : "Saved."}</Notice>}
      <p className="m-0 text-sm text-fg-muted">
        {zh
          ? "响应开始后不切换模型；无法完整转移的会话状态仍返回原错误。实际目标可在请求日志中查看。"
          : "Once a response starts, its model does not change. Requests with non-portable conversation state keep the original error. Request logs show the actual target."}
      </p>
      {state.last && (
        <div className="text-sm">
          <strong>{zh ? "最近一次切换" : "Last fallback"}</strong>
          <p className="break-all">
            {state.last.from} → {state.last.to} · {state.last.trigger} · {state.last.outcome}
            {state.last.reason ? ` · ${state.last.reason}` : ""}
          </p>
        </div>
      )}
    </>
  );
}
