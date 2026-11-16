import { readManagementError } from "./fetch-json";
export async function apiErrorMessage(response: Response, fallback: string): Promise<string> {
  return (await readManagementError(response, fallback)).message;
}
