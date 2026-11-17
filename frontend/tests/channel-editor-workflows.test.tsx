import { act } from "react";
import { afterEach, expect, test, vi } from "vitest";
import {
  clearClientResourceStoresForTests,
  invalidateClientResource,
} from "../src/client-resource";
import type { ChannelDetail } from "../src/contracts/channels";
import { en } from "../src/i18n/en";
import { LanguageProvider } from "../src/i18n/provider";
import Providers from "../src/pages/Providers";
import { button, click, deferred, fill, mount } from "./dom";

afterEach(clearClientResourceStoresForTests);

test.each([1, 2])(
  "editing survives background refresh at revision %s; failed save keeps the draft and retry sends it with the reviewed revision",
  async (nextRevision) => {
    const detail: ChannelDetail = {
      revision: 1,
      channel: {
        id: "demo",
        name: "Demo",
        service_id: "custom",
        protocol: "openai-chat",
        auth_mode: "key",
        endpoints: { base_url: "https://demo.invalid/v1" },
      },
      credential: { state: "configured" },
      has_custom_headers: false,
      discovery: { state: "not_checked" },
    };
    let refreshing = false;
    const refreshed = deferred<Response>();
    let save = deferred<Response>();
    const writes: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (init?.method === "PUT") {
          writes.push({ url: path, body: JSON.parse(String(init.body)) });
          return save.promise;
        }
        if (path === "/api/channels")
          return refreshing
            ? refreshed.promise
            : Response.json({ revision: 1, channels: [detail], default_channel_id: "demo" });
        if (path === "/api/services") return Response.json({ providers: [] });
        if (path === "/api/models") return Response.json([]);
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    const view = await mount(
      <LanguageProvider>
        <Providers apiBase="" />
      </LanguageProvider>,
    );
    await click(button(view.host, en["channels.edit"]));
    const name = () =>
      [...view.host.querySelectorAll("label")]
        .find((label) => label.textContent?.startsWith(en["channels.name"]))!
        .querySelector("input")!;
    await fill(name(), "Unsaved personal name");
    expect(name().value).toBe("Unsaved personal name");
    refreshing = true;
    await act(async () => invalidateClientResource("channels:"));
    expect(name().value).toBe("Unsaved personal name");
    await act(async () =>
      refreshed.resolve(
        Response.json({
          revision: nextRevision,
          channels: [{ ...detail, revision: nextRevision }],
          default_channel_id: "demo",
        }),
      ),
    );
    expect(name().value).toBe("Unsaved personal name");
    await click(button(view.host, en["channels.save"]));
    expect(writes[0]).toMatchObject({
      url: "/api/channels/demo?revision=1",
      body: { channel: { name: "Unsaved personal name" } },
    });
    expect(button(view.host, en["channels.save"]).disabled).toBe(true);
    await act(async () =>
      save.resolve(
        Response.json(
          {
            error: {
              code: "conflict",
              message: "Channel changed; reload before saving",
              retryable: false,
            },
          },
          { status: 409 },
        ),
      ),
    );
    expect(view.host.textContent).toContain("Channel changed; reload before saving");
    expect(name().value).toBe("Unsaved personal name");
    // Reopening is the user's explicit decision to discard the old draft and review new data.
    await click(button(view.host, en["channels.cancel"]));
    await click(button(view.host, en["channels.edit"]));
    expect(name().value).toBe("Demo");
    await fill(name(), "Resolved personal name");
    save = deferred<Response>();
    await click(button(view.host, en["channels.save"]));
    expect(writes).toHaveLength(2);
    expect(writes[1]).toMatchObject({
      url: `/api/channels/demo?revision=${nextRevision}`,
      body: { channel: { name: "Resolved personal name" }, credential: { action: "keep" } },
    });
    await act(async () =>
      save.resolve(
        Response.json({
          channel: { id: "demo" },
          application: {
            status: "failed",
            clients: { codex: { ok: false, message: "Resolve client configuration conflict" } },
          },
        }),
      ),
    );
    expect(view.host.querySelector(".channel-editor")).toBeNull();
    expect(view.host.textContent).toContain("Resolve client configuration conflict");
  },
);
