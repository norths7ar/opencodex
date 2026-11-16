/** A provider has one login flow. Starts and cancels remain ordered across page instances. */
const pending = new Map<string, Promise<unknown>>();
export function orderOAuthCommand<T>(apiBase: string, command: () => Promise<T>): Promise<T> {
  const result = (pending.get(apiBase) ?? Promise.resolve()).catch(() => undefined).then(command);
  pending.set(apiBase, result);
  void result
    .finally(() => {
      if (pending.get(apiBase) === result) pending.delete(apiBase);
    })
    .catch(() => undefined);
  return result;
}
