export type OAuthFlow = "browser" | "device" | "code";
const choices = new Map<string, OAuthFlow>();

export function readOAuthFlow(provider = "openai"): OAuthFlow {
  if (provider === "kimi" || provider === "kimi-responses") return "device";
  const choice = choices.get(provider);
  if (choice) return choice;
  const alternative = provider === "anthropic" ? "code" : "device";
  try {
    return localStorage.getItem(`ocx.oauth.${provider}.flow`) === alternative
      ? alternative
      : "browser";
  } catch {
    return "browser";
  }
}

export function writeOAuthFlow(flow: OAuthFlow, provider = "openai"): void {
  choices.set(provider, flow);
  try {
    localStorage.setItem(`ocx.oauth.${provider}.flow`, flow);
  } catch {
    /* Session choice still applies. */
  }
}
