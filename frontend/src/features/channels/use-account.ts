import type { AccountQuota } from "@/codex-quota-utils";
import { useDataSurface } from "@/data-surface";
import { readJsonOrThrow } from "@/fetch-json";
import { useCallback, useEffect, useRef, useState } from "react";
import { orderOAuthCommand } from "./oauth-commands";

type LoginHint = { url?: string; instructions?: string; deviceCode?: string };
type Account = {
  id: string;
  email?: string;
  alias?: string;
  quota?: AccountQuota | null;
  plan?: string;
  needsReauth?: boolean;
};
type Status = {
  loggedIn: boolean;
  pending?: boolean;
  done?: boolean;
  error?: string;
  email?: string;
  hint?: LoginHint;
  accounts?: Account[];
};

export function useChatGptAccount(apiBase: string, onChanged: () => void) {
  const [hint, setHint] = useState<LoginHint | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = useRef(false);
  const starting = useRef(false);
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const changed = useRef(onChanged);
  useEffect(() => {
    changed.current = onChanged;
  }, [onChanged]);
  const status = useDataSurface(
    `chatgpt-status:${apiBase}`,
    [apiBase],
    (signal) =>
      fetch(`${apiBase}/api/oauth/status?provider=openai`, { signal }).then(
        readJsonOrThrow<Status>,
      ),
    { isEmpty: () => false, pollMs: hint ? 2000 : 30_000 },
  );
  const cancel = useCallback(async () => {
    generation.current++;
    active.current = false;
    const response = await orderOAuthCommand(apiBase, () =>
      fetch(`${apiBase}/api/oauth/login/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: "openai" }),
      }),
    );
    await readJsonOrThrow(response);
    setHint(null);
    setBusy(false);
  }, [apiBase]);
  useEffect(
    () => () => {
      generation.current++;
      if (active.current)
        void orderOAuthCommand(apiBase, () =>
          fetch(`${apiBase}/api/oauth/login/cancel`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: "openai" }),
            keepalive: true,
          }),
        ).catch(() => undefined);
    },
    [apiBase],
  );
  useEffect(() => {
    if (!active.current || starting.current || !status.data) return;
    const value = status.data;
    if (value.pending) return;
    if (!value.done && !value.error) return;
    active.current = false;
    queueMicrotask(() => {
      setHint(null);
      setBusy(false);
      setError(value.error ?? null);
      setRevision((value) => value + 1);
      changed.current();
    });
  }, [status.data]);
  const login = async (flow: "browser" | "device") => {
    const ticket = ++generation.current;
    setBusy(true);
    active.current = true;
    starting.current = true;
    setError(null);
    try {
      const result = await orderOAuthCommand(apiBase, () =>
        fetch(`${apiBase}/api/oauth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider: "openai", flow, openBrowser: true }),
        }).then(readJsonOrThrow<LoginHint>),
      );
      if (ticket !== generation.current) return;
      starting.current = false;
      setHint(result ?? {});
      status.refresh();
    } catch (cause) {
      if (ticket === generation.current) {
        active.current = false;
        starting.current = false;
        setBusy(false);
        setError(String(cause));
      }
    }
  };
  const logout = async () => {
    ++generation.current;
    active.current = false;
    starting.current = false;
    setBusy(true);
    setHint(null);
    try {
      await orderOAuthCommand(apiBase, async () => {
        await fetch(`${apiBase}/api/oauth/login/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider: "openai" }),
        }).then(readJsonOrThrow);
        await fetch(`${apiBase}/api/oauth/logout?provider=openai`, { method: "POST" }).then(
          readJsonOrThrow,
        );
      });
      status.refresh();
      setRevision((value) => value + 1);
      changed.current();
    } finally {
      setBusy(false);
    }
  };
  const submit = async (code: string) => {
    const ticket = generation.current;
    await orderOAuthCommand(apiBase, async () => {
      if (ticket !== generation.current || !active.current) return;
      await fetch(`${apiBase}/api/oauth/login/code`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: "openai", code }),
      }).then(readJsonOrThrow);
    });
    if (ticket === generation.current) status.refresh();
  };
  return {
    revision,
    status,
    hint: status.data?.hint ?? hint,
    busy,
    error,
    login,
    logout,
    cancel,
    submit,
  };
}
