import { mapToolChoice } from "../../responses/parser-tools";
import { extractHostedWebSearch } from "../../web-search/synthetic-tool";
import { modelInList, type OcxProviderConfig, type OcxTool } from "../../types";
import { hasResponseMediaType } from "../../lib/http-response-semantics";
import { applyReplayRefusalClientHeaders, isNonReplayableResponse, markResponseNonReplayable } from "../../lib/upstream-retry";
import { clientWireOf } from "../inference/client-wire";
import { isPreinspectedJsonResponse, markPreinspectedJsonResponse } from "../relay";
import { isAuthorizedProviderExecutedCall } from "../responses-undeclared-tool-guard";
import { carryResponseToolOwnership, responseToolOwnership } from "./response-tool-ownership";

const allowedHostedSelectors = new Set([
  "web_search", "web_search_preview", "file_search", "computer_use_preview", "computer_use",
  "code_interpreter", "image_generation", "image_gen", "tool_search",
]);

function knownToolSelector(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const entry = value as Record<string, unknown>;
  if (entry.type === "mcp") return typeof entry.server_label === "string" && !!entry.server_label.trim()
    && (entry.name === undefined || entry.name === null || (typeof entry.name === "string" && !!entry.name.trim()));
  if (entry.name !== undefined && (typeof entry.name !== "string" || !entry.name.trim())) return false;
  if (entry.type === "function" || entry.type === "custom") return typeof entry.name === "string";
  return typeof entry.type === "string" && allowedHostedSelectors.has(entry.type);
}

/** Only definite, well-formed mandatory selectors justify excluding a destination. */
function hasMandatoryToolChoice(rawBody: unknown): boolean {
  if (!rawBody || typeof rawBody !== "object" || Array.isArray(rawBody)) return false;
  const choice = (rawBody as Record<string, unknown>).tool_choice;
  if (choice === "required") return true;
  if (!choice || typeof choice !== "object" || Array.isArray(choice)) return false;
  const selector = choice as Record<string, unknown>;
  if (selector.type !== "allowed_tools") return knownToolSelector(selector);
  return selector.mode === "required"
    && Array.isArray(selector.tools) && selector.tools.length > 0
    && selector.tools.every(knownToolSelector);
}

/** A translated Chat target must not turn a mandatory selector into auto or none. */
export function chatCanPreserveMandatoryToolChoice(rawBody: unknown, provider: OcxProviderConfig, modelId: string, effectiveTools?: readonly OcxTool[]): boolean {
  if (!hasMandatoryToolChoice(rawBody)) return true;
  if (modelInList(provider.autoToolChoiceOnlyModels, modelId)) return false;
  const body = rawBody as Record<string, unknown>;
  const choice = body.tool_choice;
  const tools = body.tools;
  const hasDeclaredTools = Array.isArray(tools) && tools.length > 0
    || Array.isArray(body.input) && body.input.some(item => item && typeof item === "object"
      && (item.type === "additional_tools" || item.type === "tool_search_output")
      && Array.isArray(item.tools) && item.tools.length > 0);
  // The parsed catalog includes additional_tools and tool_search_output. A static
  // candidate check without it defers this decision to final request preparation.
  // Hosted search belongs to the later sidecar injection owner.
  if (choice === "required" && hasDeclaredTools && effectiveTools !== undefined && effectiveTools.length === 0
    && !extractHostedWebSearch(Array.isArray(tools) ? tools : undefined)) return false;
  if (choice && typeof choice === "object" && !Array.isArray(choice)) {
    const selector = choice as Record<string, unknown>;
    // Chat function names cannot preserve the selected MCP server's ownership.
    if (selector.type === "allowed_tools" && Array.isArray(selector.tools)
      && selector.tools.some(tool => tool && typeof tool === "object" && tool.type === "mcp")) return false;
  }
  const mapped = mapToolChoice(choice);
  return mapped === "required" || (!!mapped && typeof mapped === "object");
}

const hostedCalls = new Map([
  ["web_search", "web_search_call"], ["web_search_preview", "web_search_call"],
  ["file_search", "file_search_call"], ["code_interpreter", "code_interpreter_call"],
  ["image_generation", "image_generation_call"], ["image_gen", "image_generation_call"],
  ["mcp", "mcp_call"], ["computer_use", "computer_call"],
  ["computer_use_preview", "computer_call"],
]);
