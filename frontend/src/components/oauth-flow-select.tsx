import { useState } from "react";
import { useI18n } from "../i18n/shared";
import { useRuntimeCapabilities } from "../runtime-capabilities";
import { readOAuthFlow, writeOAuthFlow, type OAuthFlow } from "../oauth-flow-pref";
import { Select } from "../ui";

export function OAuthFlowSelect({
  provider,
  disabled = false,
}: {
  provider: string;
  disabled?: boolean;
}) {
  const capabilities = useRuntimeCapabilities();
  if (
    !["openai", "anthropic"].includes(provider) ||
    !capabilities?.oauthProviders?.includes(provider)
  )
    return null;
  return <ProviderOAuthFlowSelect key={provider} provider={provider} disabled={disabled} />;
}

function ProviderOAuthFlowSelect({ provider, disabled }: { provider: string; disabled: boolean }) {
  const { locale } = useI18n();
  const zh = locale === "zh" || locale === "zh-TW";
  const [flow, setFlow] = useState(() => readOAuthFlow(provider));
  const alternative =
    provider === "anthropic"
      ? { value: "code", label: zh ? "粘贴授权码" : "Paste authorization code" }
      : { value: "device", label: zh ? "设备码授权" : "Device code" };
  return (
    <div className="oauth-flow-select">
      <Select
        label={zh ? "授权方式" : "Authorization method"}
        value={flow}
        disabled={disabled}
        options={[
          { value: "browser", label: zh ? "浏览器授权" : "Browser authorization" },
          alternative,
        ]}
        onChange={(value) => {
          const next = value as OAuthFlow;
          setFlow(next);
          writeOAuthFlow(next, provider);
        }}
      />
    </div>
  );
}
