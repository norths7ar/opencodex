/** Distinguish a missing/failed page module from an ordinary render error. */
export class PageLoadError extends Error {
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
    this.name = "PageLoadError";
  }
}

export function pageImport<T>(load: () => Promise<T>): () => Promise<T> {
  return () =>
    load().catch((cause: unknown) => {
      throw new PageLoadError(cause);
    });
}
