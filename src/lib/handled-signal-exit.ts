/** A completed signal-driven shutdown is a clean exit on supported platforms. */
export function handledSignalExitCode(
  signal: NodeJS.Signals | undefined,
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): number {
  return 0;
}
