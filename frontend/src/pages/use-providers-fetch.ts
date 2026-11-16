import { useCallback, useEffect, useRef, useState } from "react";
import type { TFn } from "../i18n/shared";
import { loadProviderWorkspace } from "../channel-management";
import { readJsonOrThrow } from "../fetch-json";
import { writeSessionListCache } from "../session-list-cache";
import type { OAuthStatus, ProvidersConfig } from "./providers-shared";
import { useRuntimeCapabilities } from "../runtime-capabilities";
export type ProvidersConfigRefreshResult = "applied" | "failed" | "superseded";

export function useProvidersFetch({
  apiBase,
  t,
  setConfig,
  setOauthProviders,
  setOauthStatus,
  notify,
  invalidateProviderQuotas,
  configCacheKey,
}: {
  apiBase: string;
  t: TFn;
  setConfig: React.Dispatch<React.SetStateAction<ProvidersConfig | null>>;
  setOauthProviders: React.Dispatch<React.SetStateAction<string[]>>;
  setOauthStatus: React.Dispatch<React.SetStateAction<Record<string, OAuthStatus>>>;
  notify: (msg: string, ok: boolean) => void;
  /** Bump the shell's quota revision; `force` adds `?refresh=1` to its next read. */
  invalidateProviderQuotas: (force?: boolean) => void;
  /** Session seed key for instant Providers shell paint (no secrets — hasApiKey flags only). */
  configCacheKey?: string;
}) {
  const capabilities = useRuntimeCapabilities();
  const configRequest = useRef(0);
  const [configRead, setConfigRead] = useState<{
    apiBase: string;
    state: "loading" | "ready" | "error";
  }>();
  const oauthRequest = useRef(0);
  useEffect(
    () => () => {
      configRequest.current += 1;
      oauthRequest.current += 1;
    },
    [apiBase],
  );
  const fetchConfig = useCallback(async (): Promise<ProvidersConfigRefreshResult> => {
    const request = ++configRequest.current;
    setConfigRead({ apiBase, state: "loading" });
    try {
      const data = await loadProviderWorkspace(apiBase);
      if (request !== configRequest.current) return "superseded";
      if (!data) throw new Error("config response missing");
      setConfig(data ?? null);
      setConfigRead({ apiBase, state: "ready" });
      if (configCacheKey && data) writeSessionListCache(configCacheKey, data);
      return "applied";
    } catch {
      if (request !== configRequest.current) return "superseded";
      setConfigRead({ apiBase, state: "error" });
      notify(t("prov.loadConfigFail"), false);
      return "failed";
    }
  }, [apiBase, configCacheKey, notify, setConfig, t]);

  const fetchOauth = useCallback(async () => {
    if (capabilities?.oauth === false) return;
    const request = ++oauthRequest.current;
    const markFailed = (providers: string[]) => {
      if (request !== oauthRequest.current) return;
      setOauthStatus((previous) => {
        const next = { ...previous };
        for (const provider of providers) {
          next[provider] = { ...previous[provider], statusCheck: "failed" };
        }
        return next;
      });
    };
    let providers = capabilities?.oauthProviders ?? [];
    try {
      // Every channel alias points at the same ChatGPT account. Read the grant once.
      const [directory, grant] = await Promise.all([
        fetch(`${apiBase}/api/oauth/providers`).then(readJsonOrThrow<{ providers?: string[] }>),
        fetch(`${apiBase}/api/oauth/status?provider=openai`).then(readJsonOrThrow<OAuthStatus>),
      ]);
      providers = directory?.providers ?? providers;
      if (request !== oauthRequest.current) return;
      setOauthProviders(providers);
      if (typeof grant?.loggedIn !== "boolean") throw new Error("Missing login status");
      setOauthStatus((previous) => ({
        ...previous,
        ...Object.fromEntries(
          providers.map((name) => [name, { ...grant, statusCheck: "ready" as const }]),
        ),
      }));
    } catch {
      markFailed(providers);
    }
  }, [
    apiBase,
    capabilities?.oauth,
    capabilities?.oauthProviders,
    setOauthProviders,
    setOauthStatus,
  ]);

  /*
   * The workspace shell owns the single quota read; this only invalidates it. Keeping the
   * name means all twelve existing mutation call sites keep working unchanged, and a
   * mutation can no longer race the shell's own fetch for the same data.
   */
  const fetchProviderQuotas = useCallback(
    async (refresh = false) => {
      invalidateProviderQuotas(refresh);
    },
    [invalidateProviderQuotas],
  );

  return {
    fetchConfig,
    fetchOauth,
    fetchProviderQuotas,
    configLoadState: configRead?.apiBase === apiBase ? configRead.state : "loading",
  };
}
