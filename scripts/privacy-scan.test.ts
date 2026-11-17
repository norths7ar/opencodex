// Offline detector checks: node --test scripts/privacy-scan.test.ts
// These test scanText, not the legacy Bun CLI's file selection or repository exemptions.
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { scanText } from "./privacy-scan.ts";

const kinds = (text: string) => scanText("sample.txt", text).map((finding) => finding.kind);

describe("sensitive text detection", () => {
  it("detects credential formats without storing real credentials", () => {
    const probes = [
      ["bearer-token", "Bearer " + "x".repeat(28)],
      ["token-looking", "sk-" + "a".repeat(24)],
      ["token-looking", "ghp_" + "b".repeat(24)],
      ["token-looking", "eyJ" + "c".repeat(24) + "." + "d".repeat(24)],
      ["meta-api-key", ["LLM", "1".repeat(16), "e".repeat(27)].join("|")],
    ];
    for (const [kind, value] of probes) {
      assert.ok(kinds(`credential = "${value}"`).includes(kind), kind);
    }
    assert.equal(kinds("Keys may start with sk- or LLM|.").length, 0);
  });

  it("locates findings in the supplied document", () => {
    const key = "sk-" + "a".repeat(24);
    const findings = scanText("candidate.py", `# example\ncredential = "${key}"\n`);
    const token = findings.find((finding) => finding.kind === "token-looking");
    assert.ok(token);
    assert.equal(token.file, "candidate.py");
    assert.equal(token.line, 2);
  });

  it("recognizes personal paths and email while accepting reserved examples", () => {
    assert.ok(kinds("/Users/private-user/project/file").includes("home-path"));
    assert.ok(kinds(["contact", "internal-company.net"].join("@")).includes("email"));
    assert.equal(kinds(["contact", "example.com"].join("@")).includes("email"), false);
  });

  it("distinguishes scaled raster filenames from email addresses", () => {
    for (const asset of ["128x128@2x.png", "icon_16x16@2x.png", "icon_512x512@2x.png"]) {
      assert.equal(kinds(`"${asset}"`).includes("email"), false, asset);
    }
    for (const [local, domain] of [
      ["alice", "2x.png"],
      ["128x128", "2x.com"],
      ["128x128", "4x.png"],
      ["128x128", "2x.internal-company.net"],
    ]) {
      assert.ok(kinds([local, domain].join("@")).includes("email"));
    }
  });
});

describe("SSH configuration detection", () => {
  it("recognizes endpoint and proxy directives including comments and substitutions", () => {
    for (const line of [
      "HostName runner.internal-company.net",
      "HostName runner.internal-company.net # build host",
      "HostName example.com.internal-company.net",
      "HostName root@runner:2222",
    ]) {
      assert.ok(kinds(line).includes("ssh-endpoint"), line);
    }
    for (const line of [
      "ProxyCommand /opt/bin/cloudflared access ssh --hostname %h",
      "ProxyCommand=cloudflared access ssh --hostname runner.internal-company.net",
      "ProxyCommand nc -X connect -x proxy.example.com:8080 runner.internal-company.net 22",
      "ProxyCommand $CF access ssh --hostname runner.internal-company.net",
      "ProxyCommand %h runner.internal-company.net",
    ]) {
      assert.ok(kinds(line).includes("ssh-proxy-command"), line);
    }
  });

  it("allows wholly templated or reserved SSH examples", () => {
    for (const line of [
      "HostName example.com # sample",
      "HostName <your-runner>",
      "HostName $RUNNER_HOST",
      "HostName localhost",
      "ProxyCommand %h",
      "ProxyCommand %r@%h:%p",
      "ProxyCommand <your-proxy>",
      "ProxyCommand ${PROXY}",
    ]) {
      assert.equal(
        kinds(line).some((kind) => kind.startsWith("ssh-")),
        false,
        line,
      );
    }
  });

  it("does not confuse ordinary prose or assignments with SSH configuration", () => {
    for (const line of [
      "User aliases are display metadata.",
      "user configuration.",
      "The hostname is resolved by the adapter.",
      'hostname = "127.0.0.1",',
      "hostname === undefined ? { host } : { host, hostname },",
    ]) {
      assert.equal(
        kinds(line).some((kind) => kind.startsWith("ssh-")),
        false,
        line,
      );
    }
  });
});
