import { confirmAction } from "@/action-dialogs";
import type { AccountQuota } from "@/codex-quota-utils";
import QuotaBars from "@/components/QuotaBars";
import { Button } from "@/components/button";
import { useDataSurface } from "@/data-surface";
import { readJsonOrThrow } from "@/fetch-json";
import { useT } from "@/i18n/shared";
import { IconKey, IconRefresh } from "@/icons";
import { Notice } from "@/ui";
import { useEffect, useId, useRef, useState } from "react";
import { useChannelCopy } from "./copy";
import { useChatGptAccount } from "./use-account";

export function AccountPanel({ apiBase, onChanged }: { apiBase: string; onChanged: () => void }) {
  const c = useChannelCopy();
  const t = useT();
  const account = useChatGptAccount(apiBase, onChanged);
  const [choosingMethod, setChoosingMethod] = useState(false);
  const [flow, setFlow] = useState<"browser" | "device" | null>(null);
  const chooserId = useId();
  const loginButton = useRef<HTMLButtonElement>(null);
  const browserButton = useRef<HTMLButtonElement>(null);
  const [callback, setCallback] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  useEffect(() => {
    if (choosingMethod) browserButton.current?.focus();
  }, [choosingMethod]);
  const quota = useDataSurface(
    `chatgpt-quota:${apiBase}:${account.status.data?.email ?? ""}:${account.revision}`,
    [apiBase, account.status.data?.loggedIn, account.status.data?.email, account.revision],
    (signal) =>
      fetch(`${apiBase}/api/oauth/accounts?provider=openai&quota=1`, { signal }).then(
        readJsonOrThrow<{ accounts: Array<{ quota?: AccountQuota; planLabel?: string }> }>,
      ),
    { isEmpty: () => false, enabled: account.status.data?.loggedIn === true },
  );
  const run = async (operation: () => Promise<unknown>) => {
    setError(null);
    try {
      await operation();
    } catch (cause) {
      setError(String(cause));
    }
  };
  const refreshQuota = async () => {
    setRefreshing(true);
    try {
      await fetch(`${apiBase}/api/oauth/accounts?provider=openai&quota=1&refresh=1`).then(
        readJsonOrThrow,
      );
      quota.refresh();
    } finally {
      setRefreshing(false);
    }
  };
  const beginLogin = (method: "browser" | "device") => {
    setChoosingMethod(false);
    setCallback("");
    setFlow(method);
    setError(null);
    void account.login(method);
  };
  const closeChooser = () => {
    setChoosingMethod(false);
    loginButton.current?.focus();
  };
  return (
    <section className="channel-panel channel-account">
      <div className="channel-account-heading">
        <div>
          <h3 className="channel-section-title">
            <IconKey aria-hidden="true" />
            {c("account")}
          </h3>
          <p
            className="channel-state"
            data-tone={account.status.data?.loggedIn ? "ready" : "quiet"}
          >
            {account.status.data?.email ??
              (account.status.state.showSkeleton ? c("loading") : c("loginRequired"))}
          </p>
        </div>
        <div className="channel-actions">
          <Button
            ref={loginButton}
            disabled={account.busy}
            aria-expanded={choosingMethod}
            aria-controls={choosingMethod ? chooserId : undefined}
            onClick={() => setChoosingMethod(!choosingMethod)}
          >
            {c(account.status.data?.loggedIn ? "replace" : "login")}
          </Button>
          {account.status.data?.loggedIn && (
            <Button
              disabled={account.busy}
              variant="danger"
              onClick={() =>
                void run(async () => {
                  if (
                    await confirmAction({
                      message: c("logoutConfirm"),
                      confirmLabel: c("logout"),
                      tone: "danger",
                    })
                  ) {
                    setChoosingMethod(false);
                    setFlow(null);
                    await account.logout();
                  }
                })
              }
            >
              {c("logout")}
            </Button>
          )}
        </div>
      </div>
      {choosingMethod && !account.busy && (
        <div
          className="channel-login-choice"
          id={chooserId}
          role="group"
          aria-labelledby={`${chooserId}-title`}
        >
          <h4 id={`${chooserId}-title`}>{c("loginMethod")}</h4>
          <p>{c("deviceHelp")}</p>
          <div className="channel-actions">
            <Button
              ref={browserButton}
              size="sm"
              variant="primary"
              onClick={() => beginLogin("browser")}
            >
              {c("browser")}
            </Button>
            <Button size="sm" onClick={() => beginLogin("device")}>
              {c("device")}
            </Button>
            <Button size="sm" variant="ghost" onClick={closeChooser}>
              {c("cancel")}
            </Button>
          </div>
        </div>
      )}
      {flow && account.busy && (
        <div className="channel-login">
          <h3>{c("pending")}</h3>
          {account.hint?.url && (
            <a href={account.hint.url} target="_blank" rel="noreferrer">
              {account.hint.url}
            </a>
          )}
          {account.hint?.deviceCode && <code>{account.hint.deviceCode}</code>}
          {account.hint?.instructions && <p>{account.hint.instructions}</p>}
          {flow === "browser" && (
            <label>
              {c("callback")}
              <input
                className="input"
                value={callback}
                onChange={(e) => setCallback(e.target.value)}
              />
            </label>
          )}
          <div className="channel-actions">
            <Button onClick={() => void run(account.cancel)}>{c("cancel")}</Button>
            {flow === "browser" && (
              <Button
                disabled={!callback.trim()}
                onClick={() => void run(() => account.submit(callback))}
              >
                {c("submit")}
              </Button>
            )}
          </div>
        </div>
      )}
      {(error || account.error || account.status.state.showError) && (
        <Notice tone="err">{error ?? account.error ?? c("error")}</Notice>
      )}
      {account.status.data?.loggedIn && (
        <section className="channel-quota">
          <div className="channel-account-heading">
            <h3>{c("quota")}</h3>
            <Button
              size="sm"
              disabled={refreshing || quota.state.refreshing}
              onClick={() => void run(refreshQuota)}
            >
              <IconRefresh aria-hidden="true" />
              {c("refreshQuota")}
            </Button>
          </div>
          {quota.state.showSkeleton || quota.data?.accounts?.[0]?.quota ? (
            <QuotaBars
              quota={quota.data?.accounts?.[0]?.quota ?? null}
              threshold={80}
              t={t}
              layout="stacked"
              pending={quota.state.showSkeleton}
            />
          ) : (
            <p className="muted">{c("quotaUnavailable")}</p>
          )}
          {quota.state.showError && <Notice tone="warn">{c("error")}</Notice>}
        </section>
      )}
    </section>
  );
}
