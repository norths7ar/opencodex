import { useT } from "../i18n/shared";
import { Select, type SelectOption } from "../ui";

export function SettingToggle({
  label,
  checked,
  onChange,
  disabled = false,
  describedBy,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  describedBy?: string;
}) {
  return (
    <label className="toggle">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-label={label}
        aria-describedby={describedBy}
        onChange={event => onChange(event.target.checked)}
      />
      <span className="slider" aria-hidden="true" />
    </label>
  );
}

export function SmallFastModelSetting({
  value,
  tierHaikuModel,
  options,
  onChange,
}: {
  value: string;
  tierHaikuModel?: string;
  options: SelectOption[];
  onChange: (value: string) => void;
}) {
  const t = useT();
  const effectiveHelperModel = tierHaikuModel ?? value;
  // Title lives in ClaudeCode's shared ccw-main-head when mounted in the workspace rail.
  return (
    <>
      <p className="muted text-label" style={{ margin: "0 0 8px" }}>
        {t("claude.smallFastModelAccurateHint")}
      </p>
      <Select
        value={value}
        options={options}
        onChange={onChange}
        label={t("claude.smallFastModel")}
        style={{ maxWidth: 420 }}
      />
      {effectiveHelperModel === "" && (
        <p className="notice-warn" role="status" style={{ marginTop: 8 }}>
          {t("claude.smallFastModelNativeWarning")}
        </p>
      )}
    </>
  );
}
