import { expect, test } from "bun:test";
import { candidateFailureDecision } from "../../src/routing/candidate-failure";

const message = "Your credit balance is too low to access the Anthropic API.";
const envelope = (type = "invalid_request_error", text = message, code?: string) => JSON.stringify({
  type: "error", error: { type, message: text, ...(code !== undefined ? { code } : {}) },
});

test("billing prose without an accepted code, malformed JSON and unrelated refusals stay terminal", () => {
  for (const carrier of [message, envelope().slice(0, -1), envelope("invalid_request_error", `Diagnostic: ${message}`),
    envelope("invalid_request_error", `${message} Model denied.`), envelope("api_error"),
    envelope("invalid_request_error", message, "context_length_exceeded"),
    JSON.stringify({ response: JSON.parse(envelope()) })]) {
    expect(candidateFailureDecision(400, carrier)).toBe("stop");
  }
  for (const code of ["context_length_exceeded", "invalid_api_key", "unsupported_parameter"]) {
    expect(candidateFailureDecision(400, message, { code })).toBe("stop");
  }
  expect(candidateFailureDecision(499, envelope(), { code: "invalid_request_error" })).toBe("stop");
  expect(candidateFailureDecision(400, envelope(), { code: "origin_rejected" })).toBe("stop");
});
