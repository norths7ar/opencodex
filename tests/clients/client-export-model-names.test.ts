import { describe, expect, test } from "bun:test";
import { buildClientConfig, buildClientConfigText, EXPORT_CLIENT_IDS } from "../../backend/src/clients/config-export";
import { previewRouteModel } from "../../backend/src/router";
import { parseSyntheticRowId } from "../../backend/src/server/fast-row";
import { loadExportModels } from "../../backend/src/server/management/model-rows";
import { namingConfig, namingModels, exportedKeys, ALIAS_KEYS, ROUTED_KEYS } from "../helpers/export-model-names";

describe("client export model names", () => {
  for (const client of ["hermes", "commandcode"] as const) {
    test(`${client}: exact opt-in keys preserve every routed identity and metadata`, async () => {
      const config = namingConfig();
      const models = await namingModels(config);
      const context = { baseUrl: "http://127.0.0.1:10100/v1", models, config };
      const before = JSON.stringify(models);
      expect(exportedKeys(client, buildClientConfig(client, context))).toEqual(ROUTED_KEYS);
      const opted = { ...context, modelNames: "alias" as const };
      const alias = buildClientConfig(client, opted);
      expect(exportedKeys(client, alias)).toEqual(ALIAS_KEYS);
      expect(exportedKeys(client, buildClientConfigText(client, opted).document)).toEqual(ALIAS_KEYS);
      for (const key of exportedKeys(client, alias)) {
        const route = previewRouteModel(config, key);
        if (key === "AC-combo") { expect(route.combo).toBeDefined(); continue; }
        const original = models.find(row => row.provider === route.providerName && row.id === route.modelId);
        expect(original).toBeDefined();
        const baseline = previewRouteModel(config, original!.namespaced);
        expect([route.providerName, route.modelId]).toEqual([baseline.providerName, baseline.modelId]);
      }
      config.preferModelAliasIds = true;
      expect(exportedKeys(client, buildClientConfig(client, context))).toEqual(ROUTED_KEYS);
      expect(JSON.stringify(models)).toBe(before);
    });
  }

  test("qualified duplicate aliases stay distinct; native-ID shadows and unsafe aliases fall back", async () => {
    const config = namingConfig();
    config.providers.beta!.modelAliases = { "model-x": "AC-model-x" };
    const build = async () => {
      const ctx = { baseUrl: "http://127.0.0.1:10100/v1", config, models: await namingModels(config), modelNames: "alias" as const };
      return buildClientConfig("commandcode", ctx);
    };
    expect(exportedKeys("commandcode", await build())).toContain("beta/AC-model-x");
    config.providers.acme!.models!.push("AC-model-x");
    const shadowed = exportedKeys("commandcode", await build());
    expect(shadowed).toContain("acme/vendor-model-x");
    expect(shadowed).not.toContain("AC/AC-model-x");
    for (const unsafe of ["AC/model", "AC:model", "AC model", "AC#model", "-AC-model"]) {
      config.providers.acme!.modelAliases!["vendor/model-x"] = unsafe;
      const keys = exportedKeys("commandcode", await build());
      expect(keys).toContain("AC/AC-model-y-free");
      expect(keys).toContain("acme/vendor-model-x");
    }
  });

  test("unsupported exporters reject explicit alias policy and keep routed defaults", async () => {
    const config = namingConfig();
    const models = await namingModels(config);
    for (const client of EXPORT_CLIENT_IDS.filter(id => id !== "hermes" && id !== "commandcode")) {
      const ctx = { baseUrl: "http://127.0.0.1:10100/v1", models, config };
      const alias = { ...ctx, modelNames: "alias" as const };
      const routed = { ...ctx, modelNames: "routed" as const };
      expect(() => buildClientConfig(client, alias)).toThrow();
      expect(buildClientConfig(client, routed)).toEqual(buildClientConfig(client, ctx));
    }
  });

  test("builtin aliases, unsafe provider namespaces, preference changes and metadata stay scoped", async () => {
    const config = namingConfig();
    config.defaultModelAliases = true;
    config.providers.beta!.models = ["claude-opus-5"];
    const build = async (alias: boolean) => {
      const ctx = { baseUrl: "http://127.0.0.1:10100/v1", config, models: await namingModels(config), modelNames: alias ? "alias" as const : "routed" as const };
      return buildClientConfig("commandcode", ctx);
    };
    expect(exportedKeys("commandcode", await build(true))).toContain("beta/opus");
    config.preferModelAliasIds = true;
    expect(exportedKeys("commandcode", await build(true))).toContain("AC/AC-model-x");
    expect(exportedKeys("commandcode", await build(false))).toContain("acme/vendor-model-x");
    const alias = await build(true) as { provider: { opencodex: { models: Record<string, { contextWindow: number }> } } };
    expect(alias.provider.opencodex.models["AC/AC-model-x"]!.contextWindow).toBe(100_000);
    config.providers.acme!.alias = "unsafe/prefix";
    expect(exportedKeys("commandcode", await build(true))).toContain("acme/vendor-model-x");
  });

  test("Fast aliases preserve concrete ingress identity and exact Fast selectors cannot shadow them", async () => {
    const config = namingConfig();
    config.fastRows = true;
    config.providers.acme!.supportsServiceTier = true;
    const models = await namingModels(config);
    const ctx = { baseUrl: "http://127.0.0.1:10100/v1", config, models, modelNames: "alias" as const };
    const keys = exportedKeys("hermes", buildClientConfig("hermes", ctx));
    expect(keys).toContain("AC/AC-model-x--fast");
    const parsed = parseSyntheticRowId("AC/AC-model-x--fast", config);
    expect(parsed.fastRow?.baseId).toBe("AC/AC-model-x");
    const routed = previewRouteModel(config, parsed.fastRow!.baseId);
    expect([routed.providerName, routed.modelId]).toEqual(["acme", "vendor/model-x"]);
    config.providers.acme!.models!.push("AC-model-x--fast");
    const updated = { ...ctx, models: await namingModels(config) };
    expect(exportedKeys("hermes", buildClientConfig("hermes", updated))).toContain("acme/vendor-model-x");
  });

  for (const client of ["hermes", "commandcode"] as const) {
    test(`${client}: stale candidate metadata cannot survive alias reassignment`, async () => {
      const config = namingConfig();
      config.providers.acme!.modelAliases!["vendor/model-x"] = "pick";
      const models = await namingModels(config);
      config.providers.acme!.modelAliases = { "model-y:free": "pick" };
      const ctx = { baseUrl: "http://127.0.0.1:10100/v1", config, models, modelNames: "alias" as const };
      const keys = exportedKeys(client, buildClientConfig(client, ctx));
      expect(keys).toContain("acme/vendor-model-x");
      expect(keys).toContain("AC/pick");
      expect(keys).not.toContain("AC/AC-model-y-free");
      expect(keys).toHaveLength(5);
      const route = previewRouteModel(config, "AC/pick");
      expect([route.providerName, route.modelId]).toEqual(["acme", "model-y:free"]);
    });
  }
});
