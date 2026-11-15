/** Bare Codex selectors and the canonical openai channel address the same model. */
export function subagentModelIdentity(model: string): string {
  return model.replace(/^openai\/(?=(?:gpt-|o[134]-|codex-auto-review$))/, "");
}

/** Keep the saved spelling so rendering never rewrites an existing roster. */
export function uniqueSubagentModels(models: string[]): string[] {
  const seen = new Set<string>();
  return models.filter((model) => {
    const identity = subagentModelIdentity(model);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

export function subagentModelOptions(available: string[], chosen: string[]): string[] {
  const selected = new Map(
    uniqueSubagentModels(chosen).map((model) => [subagentModelIdentity(model), model]),
  );
  return uniqueSubagentModels(available).map(
    (model) => selected.get(subagentModelIdentity(model)) ?? model,
  );
}
