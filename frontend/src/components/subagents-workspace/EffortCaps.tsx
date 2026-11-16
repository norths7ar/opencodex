import { HelpTip } from "@/components/settings/decorations";
import { SettingsRow } from "@/components/settings/row";
import { Button } from "@/components/button";
import { useCallback, useEffect, useState } from "react";
import { readJsonOrThrow } from "../../fetch-json";
import { useT } from "../../i18n/shared";
import { Select } from "../../ui";

type Caps = { effortCap?: string | null; subagentEffortCap?: string | null };

export function EffortCaps({ apiBase }: { apiBase: string }) {
  const t = useT();
  const [caps, setCaps] = useState<Caps>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase}/api/effort-caps`);
      setCaps(await readJsonOrThrow<Caps>(response));
      setError(false);
    } catch {
      setError(true);
    }
  }, [apiBase]);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Load only commits after the network response.
    void load();
  }, [load]);
  const save = async (key: keyof Caps, value: string) => {
    setBusy(true);
    setError(false);
    try {
      const response = await fetch(`${apiBase}/api/effort-caps`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: value || null }),
      });
      setCaps(await readJsonOrThrow<Caps>(response));
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  };
  const options = [
    { value: "", label: t("dash.effortCapNone") },
    ...["low", "medium", "high", "xhigh"].map((value) => ({ value, label: value })),
  ];
  return (
    <>
      {(["effortCap", "subagentEffortCap"] as const).map((key) => {
        const label = t(
          key === "effortCap" ? "dash.effortCapLabel" : "dash.subagentEffortCapLabel",
        );
        return (
          <SettingsRow
            key={key}
            label={label}
            hint={t(
              key === "effortCap" ? "proxyV2.effortCapHint" : "proxyV2.subagentEffortCapHint",
            )}
            help={
              key === "effortCap" ? (
                <HelpTip label={label}>{t("dash.effortCapHelp")}</HelpTip>
              ) : undefined
            }
            control={
              <div style={{ visibility: caps ? undefined : "hidden" }}>
                <Select
                  label={label}
                  value={caps?.[key] ?? ""}
                  options={options}
                  disabled={!caps || busy}
                  onChange={(value) => {
                    void save(key, value);
                  }}
                />
              </div>
            }
          />
        );
      })}
      {error && (
        <div role="alert" className="px-4 py-3 text-danger">
          {t("sub.loadFail")}{" "}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              void load();
            }}
          >
            {t("common.retry")}
          </Button>
        </div>
      )}
    </>
  );
}
