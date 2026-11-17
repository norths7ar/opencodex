import { expect, test } from "vitest";
import { readJsonIfOk, readJsonOrThrow } from "../src/fetch-json";
import { apiErrorMessage } from "../src/api-error";
test("a failed management request retains the server's recovery instruction", async () => {
  const body = {
    error: {
      code: "conflict",
      message: "Choose another default before deleting",
      retryable: false,
    },
  };
  await expect(
    readJsonOrThrow(Response.json(body, { status: 409 }), "Delete failed"),
  ).rejects.toThrow(body.error.message);
  expect(await apiErrorMessage(Response.json(body, { status: 409 }), "Delete failed")).toBe(
    body.error.message,
  );
});
test("an unreadable server response produces a useful fallback instead of a JSON parsing error", async () => {
  await expect(
    readJsonOrThrow(new Response("Bad gateway", { status: 502 }), "Service unavailable"),
  ).rejects.toThrow("Service unavailable");
  expect(
    await apiErrorMessage(new Response("Bad gateway", { status: 502 }), "Service unavailable"),
  ).toBe("Service unavailable");
  expect(await readJsonIfOk(new Response("{invalid", { status: 200 }))).toBeNull();
});
