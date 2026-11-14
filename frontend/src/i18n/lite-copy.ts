import type { Locale } from "./catalogs";

const en = {
  "logs.detail.source.official": "Official pricing",
  "logs.detail.band.peak": "Peak",
  "logs.detail.band.offPeak": "Off-peak",
  "logs.detail.reason.price_time_unknown":
    "The pricing time band could not be determined from the request time and available holiday calendar.",
  "logs.detail.estimate.current_official_price":
    "Estimated from the latest retrieved official token prices, not a historical invoice. Per-use tool charges are not included.",
  "logs.detail.estimate.stale_official_price":
    "The latest price refresh failed; the last verified official prices were used.",
  "logs.detail.estimate.request_time_estimated":
    "This older record has no per-attempt pricing time; the original request time was used.",

  modelOutputLimit: "Max output",
  modelInputLimit: "Max input",
  modelContextEffective: "Effective context: {value} · Provider cap: {cap}",
  modelOutputModalities: "Output modalities",
  modelUnknown: "Unknown",
  modelNotApplicable: "Not applicable",
  modelReasoningSupported: "Supported",
  modelReasoningDisabled: "Not supported",
  modelPricing: "Pricing",
  modelPricingUnit: "USD / 1M tokens",
  modelPricingPeak: "Peak",
  modelPricingOffPeak: "Off-peak",
  modelPricingScheduleNote:
    "Usage estimates apply these rates by request time and known Chinese holidays; manual price overrides take priority.",
  modelStaleSource: "Last verified values",
  modelReasoningLevels: "Reasoning effort levels",
  modelSourceOperator: "Override",
  modelSourceUpstream: "Upstream",
  modelSourceOfficial: "Official documentation",
  modelSourcePreset: "Built-in preset",
  modelSourceFallback: "Fallback",
  modelSourceProvider: "Provider default",
  modelSourceAutomatic: "Automatic",
  modelResetOnSave: "Will follow automatic values after Save",
  modelOverridePricing: "Override automatic pricing",
  modelRestoreAll: "Restore all overrides",
  modelRestoreAllConfirm: "Restore all name, pricing and capability overrides for {model}?",
  modelIdRequired: "Enter a model ID.",
  storageFilesProcessed: "{count} files processed.",
  legacyImportPathPlaceholder: "C:\\Users\\…\\.opencodex\\config.json",
  codexRestoreChanges:
    "Restore the pre-connection configuration in {path} and its model catalog/cache, preserving other settings and unrelated later edits.",
  codexRestoreBreakage:
    "Codex will stop using this gateway, and gateway models will be removed from its model catalog.",
  codexRestoreUndo:
    "Enable the Codex connection again to reapply its configuration and rebuild the model catalog.",
  codexRestoreSideEffect:
    "The gateway will keep running, and existing chat history will remain unchanged.",
  lastChecked: "Last checked: {time}",
  refresh: "Refresh model directory",
  refreshing: "Querying upstream…",
  refreshFailed: "Model directory refresh failed.",
  retained: "Showing the last successful directory from {time}.",
  retainedUnknown: "Refresh failed; the last successful directory is retained.",
  staleDefault: "Default model is no longer listed upstream: ",
  staleAliases: "These aliases target models no longer listed upstream: ",
  chooseTarget: "Your configuration is retained. Choose a new target model.",
  discoveryVersion: "Model discovery protocol version",
  discoveryVersionHint:
    "ChatGPT filters its model directory by client version. Leave blank to use the gateway's bundled baseline; override it when a newer protocol version is required to discover new models.",
  disconnectHint:
    "Disable Codex in Connect to restore its pre-connection configuration and model catalog/cache, preserving other settings and chat history. Restart running Codex sessions to use the restored configuration; the gateway will keep running. Disabling or removing {provider} does not restore client configuration.",
} as const;

const zh: Record<keyof typeof en, string> = {
  "logs.detail.source.official": "官方定价",
  "logs.detail.band.peak": "高峰",
  "logs.detail.band.offPeak": "低峰",
  "logs.detail.reason.price_time_unknown": "无法根据请求时间和已有节假日日历确定计价时段。",
  "logs.detail.estimate.current_official_price":
    "按最近获取的官方 token 价格估算，不是历史账单；不含按次计费的工具费用。",
  "logs.detail.estimate.stale_official_price": "最新价格刷新失败，使用上次核实的官方价格。",
  "logs.detail.estimate.request_time_estimated":
    "这条旧记录没有逐次发送的计价时间，使用原请求时间估算。",

  modelOutputLimit: "最大输出",
  modelInputLimit: "最大输入",
  modelContextEffective: "生效上下文：{value} · 渠道上限：{cap}",
  modelOutputModalities: "输出模态",
  modelUnknown: "未知",
  modelNotApplicable: "不适用",
  modelReasoningSupported: "支持",
  modelReasoningDisabled: "不支持",
  modelPricing: "价格",
  modelPricingUnit: "美元／百万 token",
  modelPricingPeak: "高峰",
  modelPricingOffPeak: "低峰",
  modelPricingScheduleNote: "用量估算按请求时间和已知中国公休假日应用峰谷价格；手动价格覆盖优先。",
  modelStaleSource: "上次核实的值",
  modelReasoningLevels: "推理强度选项",
  modelSourceOperator: "手动覆盖",
  modelSourceUpstream: "上游目录",
  modelSourceOfficial: "官方文档",
  modelSourcePreset: "内置预设",
  modelSourceFallback: "回退值",
  modelSourceProvider: "渠道默认值",
  modelSourceAutomatic: "自动",
  modelResetOnSave: "保存后跟随自动值",
  modelOverridePricing: "覆盖自动价格",
  modelRestoreAll: "恢复全部覆盖",
  modelRestoreAllConfirm: "恢复 {model} 的名称、价格和能力覆盖？",
  modelIdRequired: "请输入模型 ID。",
  storageFilesProcessed: "已处理 {count} 个文件。",
  legacyImportPathPlaceholder: en.legacyImportPathPlaceholder,
  codexRestoreChanges:
    "将恢复 {path} 中接入前的配置及模型目录／缓存，并保留其他设置和后续无关编辑。",
  codexRestoreBreakage: "Codex 将不再通过此网关接入，网关提供的模型会从其模型目录中移除。",
  codexRestoreUndo: "再次启用 Codex 接入即可重新应用配置并重建模型目录。",
  codexRestoreSideEffect: "网关会继续运行，现有聊天历史保持不变。",
  lastChecked: "最近查询：{time}",
  refresh: "刷新模型目录",
  refreshing: "正在查询上游…",
  refreshFailed: "模型目录刷新失败。",
  retained: "当前保留 {time} 的目录。",
  retainedUnknown: "刷新失败，保留上次成功结果。",
  staleDefault: "默认模型已不在上游目录中：",
  staleAliases: "以下别名的目标已不在上游目录中：",
  chooseTarget: "已保留原配置，请重新选择目标模型。",
  discoveryVersion: "模型发现协议版本",
  discoveryVersionHint:
    "ChatGPT 会根据客户端版本返回模型目录。留空使用网关内置基准；新模型需要较新协议版本时，可在这里指定。",
  disconnectHint:
    "在客户端接入页面停用 Codex，可恢复接入前的配置与模型目录／缓存，保留其他设置和聊天历史。已运行的 Codex 需重新启动以使用恢复后的配置，网关会继续运行。停用或移除 {provider} 不会恢复客户端配置。",
};

/** Lite additions use the maintained English and Chinese catalogs. */
export function liteTranslator(locale: Locale) {
  const copy = locale === "zh" || locale === "zh-TW" ? zh : en;
  return (key: keyof typeof en, vars: Record<string, string> = {}) => {
    let text: string = copy[key];
    for (const [name, value] of Object.entries(vars)) text = text.replaceAll(`{${name}}`, value);
    return text;
  };
}
