import { expect, test } from "bun:test";
import {
  inspectPickerTrust, trustPickerCa, untrustPickerCa, type SecurityRunner,
} from "../../src/claude/intercept/picker-trust";

const sha1 = "A".repeat(40);

test("Linux never invokes the runner", async () => {
  const run: SecurityRunner = async () => { throw new Error("runner must stay idle"); };
  expect(await inspectPickerTrust("/leaf.pem", sha1, run, "linux")).toBe("unsupported");
  expect(await trustPickerCa("/ca.pem", run, "linux")).toEqual({ ok: false, reason: "unsupported" });
  expect(await untrustPickerCa("/ca.pem", sha1, run, "linux")).toEqual({ ok: false });
});
