import { describe, expect, spyOn, test } from "bun:test";
import { createServer } from "node:net";
import * as childProcess from "node:child_process";
import {
  listenAddressServes,
  normalizeListenAddress,
  parseListenEntriesFromNetstat,
  scanListenPidsForAddress,
  ownsIpv4LoopbackListener,
  inspectIpv4LoopbackListener,
  type LoopbackOwnerLookupIo,
  parseIpv4LoopbackListenPidsFromNetstat,
  reclaimListenPort,
  type ReclaimListenPortOptions,
} from "../../backend/src/server/port-reclaim";
import {
  isBareIpv6Address,
  parseTcpQuadsForLocalPort,
  dropWindowsTcpRowsForLocalPort,
} from "../../backend/src/server/windows-tcp-drop";
import { parseListenPidsFromNetstat } from "../../backend/src/server/port-reclaim";

/** Exercise several scans and the deadline without depending on wall-clock scheduling. */
async function reclaimWithMockClock(options: ReclaimListenPortOptions): Promise<boolean> {
  let now = 1_000;
  const clock = spyOn(Date, "now").mockImplementation(() => now);
  try {
    return await reclaimListenPort(10100, "127.0.0.1", {
      ...options,
      timeoutMs: options.timeoutMs ?? 50,
      intervalMs: options.intervalMs ?? 10,
      scanIntervalMs: options.scanIntervalMs ?? 10,
      sleepMs: async ms => {
        now += ms;
        await options.sleepMs?.(ms);
      },
    });
  } finally {
    clock.mockRestore();
  }
}

describe("parseListenPidsFromNetstat", () => {
  test("extracts Windows LISTENING owners for the local port", () => {
    const output = [
      "  Proto  Local Address          Foreign Address        State           PID",
      "  TCP    127.0.0.1:10100        0.0.0.0:0              LISTENING       18268",
      "  TCP    127.0.0.1:10100        127.0.0.1:60001        CLOSE_WAIT      18268",
      "  TCP    0.0.0.0:54321          0.0.0.0:0              LISTENING       99",
    ].join("\n");
    expect(parseListenPidsFromNetstat(output, 10100)).toEqual([18268]);
  });
});

describe("exact IPv4 loopback listener ownership", () => {

  test("Windows netstat ignores a foreign IPv6 listener on the same numeric port", () => {
    const rows = [
      "  TCP    127.0.0.1:25000        0.0.0.0:0              LISTENING       42",
      "  TCP    [::1]:25000            [::]:0                 LISTENING       99",
    ].join("\n");
    expect(parseIpv4LoopbackListenPidsFromNetstat(rows, 25000)).toEqual([42]);
  });

  test.each(["win32"] as const)("%s reports match, mismatch and ambiguous owners with boolean compatibility", async platform => {
    const row = (pid: number) => `TCP 127.0.0.1:25000 0.0.0.0:0 LISTENING ${pid}\n`;
    for (const [stdout, kind] of [[row(42), "match"], [row(43), "mismatch"],
      [row(42) + row(43), "mismatch"], ["", "mismatch"]] as const) {
      let now = 100;
      const io: LoopbackOwnerLookupIo = { platform, nowMs: () => now,
        run: async (_file, _args, timeoutMs) => {
          expect(timeoutMs).toBe(2_000);
          now += 7;
          return stdout;
        } };
      expect(await inspectIpv4LoopbackListener(25000, 42, io)).toEqual({ kind, elapsedMs: 7 });
      expect(await ownsIpv4LoopbackListener(25000, 42, io)).toBe(kind === "match");
    }
  });

  test.each([
    ["win32", {}, "lookup_error"],
  ] as const)("%s empty lsof exit 1 with overrides %j reports %s", async (platform, overrides, kind) => {
    const failure = Object.assign(new Error("Command failed: lsof"), {
      code: 1, killed: false, signal: null, stdout: "", stderr: "", ...overrides,
    });
    const io: LoopbackOwnerLookupIo = { platform, nowMs: () => 100,
      run: async () => { throw failure; } };
    expect(await inspectIpv4LoopbackListener(25000, 42, io)).toEqual({ kind, elapsedMs: 0 });
    expect(await ownsIpv4LoopbackListener(25000, 42, io)).toBe(false);
  });

  test.each([
    [Object.assign(new Error("private spawn detail"), { code: "ENOENT" }), "lookup_error"],
    [{ killed: true, signal: "SIGTERM", code: null }, "deadline"],
    [{ killed: true, signal: "SIGTERM", code: "ERR_CHILD_PROCESS_STDIO_MAXBUFFER" }, "lookup_error"],
    [{ killed: false, signal: "SIGTERM", code: null }, "lookup_error"],
    [null, "lookup_error"],
  ] as const)("rejected lookup %j returns only bounded metadata", async (failure, kind) => {
    const io: LoopbackOwnerLookupIo = { platform: "win32", nowMs: () => 100,
      run: async () => { throw failure; } };
    expect(await inspectIpv4LoopbackListener(25000, 42, io)).toEqual({ kind, elapsedMs: 0 });
    expect(await ownsIpv4LoopbackListener(25000, 42, io)).toBe(false);
  });

  test.each(["resolve", "reject"] as const)("deadline stays final after late lookup %s", async completion => {
    let now = 100;
    let expire!: () => void;
    let finish!: (stdout: string) => void;
    let fail!: (error: Error) => void;
    let cancelled = 0;
    const io: LoopbackOwnerLookupIo = { platform: "win32", nowMs: () => now,
      run: () => new Promise<string>((resolve, reject) => { finish = resolve; fail = reject; }),
      scheduleDeadline: (callback, timeoutMs) => {
        expect(timeoutMs).toBe(2_000);
        expire = callback;
        return () => { cancelled += 1; };
      } };
    const pending = inspectIpv4LoopbackListener(25000, 42, io);
    now += 2_000;
    expire();
    const result = await pending;
    expect(result).toEqual({ kind: "deadline", elapsedMs: 2_000 });
    expect(cancelled).toBe(1);
    now += 100;
    if (completion === "resolve") finish("TCP 127.0.0.1:25000 0.0.0.0:0 LISTENING 42\n");
    else fail(new Error("late private error"));
    await Promise.resolve();
    expect(await pending).toEqual({ kind: "deadline", elapsedMs: 2_000 });
    expect(result).toEqual({ kind: "deadline", elapsedMs: 2_000 });
    const booleanPending = ownsIpv4LoopbackListener(25000, 42, io);
    expire();
    expect(await booleanPending).toBe(false);
    finish("TCP 127.0.0.1:25000 0.0.0.0:0 LISTENING 42\n");
  });

  test("successful lookup cancels the deadline and retains its result", async () => {
    let expire!: () => void;
    let cancelled = false;
    const pending = inspectIpv4LoopbackListener(25000, 42, { platform: "win32", nowMs: () => 10,
      run: async () => "TCP 127.0.0.1:25000 0.0.0.0:0 LISTENING 42\n", scheduleDeadline: callback => {
        expire = callback;
        return () => { cancelled = true; };
      } });
    expect(await pending).toEqual({ kind: "match", elapsedMs: 0 });
    expect(cancelled).toBe(true);
    expire();
    expect(await pending).toEqual({ kind: "match", elapsedMs: 0 });
  });

  test.each([[0, 42], [65536, 42], [1.5, 42], [25000, 0], [25000, NaN]])(
    "invalid port/PID %j/%j fails closed before lookup", async (port, pid) => {
      expect(await inspectIpv4LoopbackListener(port, pid, { nowMs: () => 1,
        run: async () => { throw new Error("must not run"); } })).toEqual({ kind: "mismatch", elapsedMs: 0 });
    });
});

describe("listen-entry parsers keep the bound address", () => {
  test("netstat entries report each listener's local address", () => {
    const output = [
      "tcp        0      0 127.0.0.1:10100         0.0.0.0:*               LISTEN      4242/bun",
      "tcp        0      0 127.0.0.2:10100         0.0.0.0:*               LISTEN      7777/foreign",
      "tcp        0      0 127.0.0.1:22            0.0.0.0:*               LISTEN      1/sshd",
    ].join("\n");
    expect(parseListenEntriesFromNetstat(output, 10100)).toEqual([
      { pid: 4242, address: "127.0.0.1" },
      { pid: 7777, address: "127.0.0.2" },
    ]);
  });

  test("address matching treats wildcards as serving any bound address", () => {
    expect(listenAddressServes("127.0.0.1", "127.0.0.1")).toBe(true);
    expect(listenAddressServes("127.0.0.2", "127.0.0.1")).toBe(false);
    expect(listenAddressServes("0.0.0.0", "127.0.0.1")).toBe(true);
    expect(listenAddressServes("*", "127.0.0.1")).toBe(true);
    expect(listenAddressServes("::", "127.0.0.1")).toBe(true);
    expect(listenAddressServes("[::1]:443", "::1")).toBe(true);
    expect(normalizeListenAddress("::ffff:127.0.0.1")).toBe("127.0.0.1");
    expect(listenAddressServes("::ffff:127.0.0.1", "127.0.0.1")).toBe(true);
  });

  const multiAddressCases = [
    {
      name: "Windows netstat", parse: parseListenEntriesFromNetstat,
      rows: [
        "TCP 127.0.0.1:10100 0.0.0.0:0 LISTENING 4242",
        "TCP 127.0.0.2:10100 0.0.0.0:0 LISTENING 4242",
        "TCP [::ffff:127.0.0.1]:10100 [::]:0 LISTENING 4242",
      ],
    },
  ];
  for (const { name, parse, rows } of multiAddressCases) {
    for (const reverse of [false, true]) {
      test(`${name} retains all same-PID addresses with reverse=${reverse}`, () => {
        const ordered = reverse ? [...rows].reverse() : rows;
        const entries = parse([...ordered, ordered[0]].join("\n"), 10100);
        expect([...entries].sort((a, b) => a.address.localeCompare(b.address))).toEqual([
          { pid: 4242, address: "127.0.0.1" },
          { pid: 4242, address: "127.0.0.2" },
        ]);
        for (const address of ["127.0.0.1", "127.0.0.2"]) {
          expect(entries.filter(entry => listenAddressServes(entry.address, address)).map(entry => entry.pid)).toEqual([4242]);
        }
      });
    }
  }

  test("the PID-only netstat API still deduplicates multiple addresses", () => {
    expect(parseListenPidsFromNetstat(multiAddressCases[0]!.rows.join("\n"), 10100)).toEqual([4242]);
  });

  test("the address-scoped scanner filters before deduplicating same-PID listeners", () => {
    const fixture = multiAddressCases[0]!;
    for (const reverse of [false, true]) {
      const rows = reverse ? [...fixture.rows].reverse() : fixture.rows;
      const scan = spyOn(childProcess, "execFileSync").mockImplementation(() => rows.join("\n"));
      try {
        expect(scanListenPidsForAddress(10100)).toEqual({ ok: true, pids: [4242] });
        expect(scanListenPidsForAddress(10100, "127.0.0.1")).toEqual({ ok: true, pids: [4242] });
        expect(scanListenPidsForAddress(10100, "127.0.0.2")).toEqual({ ok: true, pids: [4242] });
        expect(scanListenPidsForAddress(10100, "127.0.0.3")).toEqual({ ok: true, pids: [] });
        expect(scanListenPidsForAddress(10100, "0.0.0.0")).toEqual({ ok: true, pids: [4242] });
      } finally {
        scan.mockRestore();
      }
    }
  });
});

describe("scanListenPidsForAddress (real scanner)", () => {
  test("finds this process on its own bound port and filters other addresses", async () => {
    const server = createServer();
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => resolve());
    });
    try {
      const address = server.address();
      if (typeof address === "object" && address) {
        const scan = scanListenPidsForAddress(address.port, "127.0.0.1");
        // Missing platform tools must report a failed scan rather than an empty result.
        if (scan.ok) expect(scan.pids).toContain(process.pid);
      }
    } finally {
      server.close();
    }
  });

  test("a listener on another loopback address does not serve 127.0.0.1", async () => {
    const server = createServer();
    const bound = await new Promise<boolean>(resolve => {
      server.once("error", () => resolve(false));
      server.listen(0, "127.0.0.2", () => resolve(true));
    });
    if (!bound) return;
    try {
      const address = server.address();
      if (typeof address === "object" && address) {
        const scan = scanListenPidsForAddress(address.port, "127.0.0.1");
        if (scan.ok) expect(scan.pids).not.toContain(process.pid);
        const wide = scanListenPidsForAddress(address.port, "0.0.0.0");
        if (wide.ok) expect(wide.pids).toContain(process.pid);
      }
    } finally {
      server.close();
    }
  });
});

describe("parseTcpQuadsForLocalPort / IPv6", () => {
  test("collects every TCP row on the local port including non-LISTEN states", () => {
    const output = [
      "  TCP    127.0.0.1:10100        0.0.0.0:0              LISTENING       18268",
      "  TCP    127.0.0.1:10100        127.0.0.1:60001        CLOSE_WAIT      18268",
      "  TCP    127.0.0.1:10100        127.0.0.1:62066        ESTABLISHED     18268",
      "  TCP    127.0.0.1:62066        127.0.0.1:10100        ESTABLISHED     14492",
    ].join("\n");
    expect(parseTcpQuadsForLocalPort(output, 10100)).toEqual([
      { localAddr: "127.0.0.1", localPort: 10100, remoteAddr: "0.0.0.0", remotePort: 0, state: "LISTENING" },
      { localAddr: "127.0.0.1", localPort: 10100, remoteAddr: "127.0.0.1", remotePort: 60001, state: "CLOSE_WAIT" },
      { localAddr: "127.0.0.1", localPort: 10100, remoteAddr: "127.0.0.1", remotePort: 62066, state: "ESTABLISHED" },
    ]);
  });

  test("keeps IPv6 local rows parseable without coercing them into IPv4 wildcards", () => {
    const output = [
      "  TCP    [::1]:10100            [::]:0                 LISTENING       18268",
      "  TCP    [::]:10100             [::]:0                 LISTENING       18268",
      "  TCP    127.0.0.1:10100        0.0.0.0:0              LISTENING       18268",
    ].join("\n");
    const rows = parseTcpQuadsForLocalPort(output, 10100);
    expect(rows.some(r => r.localAddr === "::1")).toBe(true);
    expect(rows.some(r => r.localAddr === "::")).toBe(true);
    expect(rows.some(r => r.localAddr === "127.0.0.1")).toBe(true);
    expect(isBareIpv6Address("::1")).toBe(true);
    expect(isBareIpv6Address("::")).toBe(true);
    expect(isBareIpv6Address("127.0.0.1")).toBe(false);
    expect(isBareIpv6Address("::ffff:127.0.0.1")).toBe(false);
  });

  test("dropWindowsTcpRowsForLocalPort never claims IPv6 rows as dropped on non-Windows", () => {
    // On non-win32 the function is a no-op; on win32 without matching rows it still
    // reports skippedIpv6 for parsed IPv6 quads when netstat is readable. Assert the
    // return shape never coerces IPv6 into a positive dropped count from IPv4 APIs alone.
    if (process.platform !== "win32") {
      expect(dropWindowsTcpRowsForLocalPort(10100)).toEqual({ dropped: 0, skippedIpv6: 0, accessDenied: 0 });
    }
  });
});

describe("reclaimListenPort", () => {
  test("does not kill any ocx listener by default (healthy proxy / stale pid files)", async () => {
    const killed: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      isAvailableFn: async () => false,
      listListenPidsFn: () => [4242],
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
  });

  test("does not kill when killOcxHolders is true but allowlist is empty", async () => {
    const killed: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: true,
      onlyKillPids: [],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [4242],
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
  });

  test("concurrent pinned-start shape: second start never kills the first ocx listener", async () => {
    const killed: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: false,
      isAvailableFn: async () => false,
      listListenPidsFn: () => [1111],
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
  });

  test("kills only the allowlisted ocx pid after revalidation", async () => {
    const killed: number[] = [];
    const verified: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: true,
      onlyKillPids: [100],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [100, 200],
      isAliveFn: () => true,
      verifyOcxFn: pid => {
        verified.push(pid);
        return pid;
      },
      killFn: pid => {
        killed.push(pid);
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([100]);
    expect(verified.filter(pid => pid === 100).length).toBeGreaterThanOrEqual(2);
  });

  test("unknown old PID: update-style reclaim kills no ocx listener", async () => {
    const killed: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: false,
      onlyKillPids: [],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [777],
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
  });

  test("does not kill foreign (non-ocx) listeners and does not drop their TCP rows", async () => {
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [999],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [555],
      isAliveFn: () => true,
      verifyOcxFn: () => null,
      killFn: pid => {
        killed.push(pid);
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test("listener-scan failure does not kill or reset TCP rows", async () => {
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [100],
      isAvailableFn: async () => false,
      listListenPidsFn: () => ({ ok: false, error: "lsof/netstat unavailable" }),
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test("ignores dead owner PIDs still listed by the OS", async () => {
    const killed: number[] = [];
    let ticks = 0;
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: true,
      onlyKillPids: [18268],
      isAvailableFn: async () => {
        ticks += 1;
        return ticks > 2;
      },
      listListenPidsFn: () => [18268],
      isAliveFn: () => false,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      sleepMs: async () => {},
    })).resolves.toBe(true);
    expect(killed).toEqual([]);
  });

  test("resets TCP rows only when no live foreign/protected listener remains", async () => {
    let available = false;
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 200,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      isAvailableFn: async () => available,
      listListenPidsFn: () => [],
      dropTcpFn: port => {
        dropped.push(port);
        available = true;
        return { dropped: 3, skippedIpv6: 1, accessDenied: 0 };
      },
      sleepMs: async () => {},
    })).resolves.toBe(true);
    expect(dropped).toEqual([10100]);
  });

  test("returns true once the port becomes available after allowlisted cleanup", async () => {
    let available = false;
    const killed: number[] = [];
    const pending = reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 500,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: true,
      onlyKillPids: [4242],
      isAvailableFn: async () => available,
      listListenPidsFn: () => (available ? [] : [4242]),
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
        available = true;
      },
      sleepMs: async () => {},
    });
    await expect(pending).resolves.toBe(true);
    expect(killed).toEqual([4242]);
  });

  test("skips kill across later scans when allowlisted pid fails revalidation", async () => {
    const killed: number[] = [];
    let available = false;
    let checks = 0;
    await expect(reclaimWithMockClock({
      dropTcpRows: false,
      killOcxHolders: true,
      onlyKillPids: [100],
      isAvailableFn: async () => available,
      listListenPidsFn: () => (available ? [] : [100]),
      isAliveFn: () => !available,
      verifyOcxFn: pid => {
        checks += 1;
        // Scan identity succeeds, then pre-kill revalidation and later scans reject it.
        return checks === 1 ? pid : null;
      },
      killFn: pid => {
        killed.push(pid);
        available = true;
      },
    })).resolves.toBe(false);
    expect(checks).toBeGreaterThanOrEqual(3);
    expect(killed).toEqual([]);
  });

  test("does not drop TCP rows across later scans after allowlisted revalidation fails", async () => {
    const killed: number[] = [];
    const dropped: number[] = [];
    let alive = true;
    let available = false;
    let checks = 0;
    await expect(reclaimWithMockClock({
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [100],
      isAvailableFn: async () => available,
      listListenPidsFn: () => (alive ? [100] : []),
      isAliveFn: () => alive,
      verifyOcxFn: pid => {
        checks += 1;
        return checks === 1 ? pid : null;
      },
      killFn: pid => {
        killed.push(pid);
        alive = false;
      },
      dropTcpFn: port => {
        dropped.push(port);
        available = true;
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
    })).resolves.toBe(false);
    expect(checks).toBeGreaterThanOrEqual(3);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test("does not kill or drop TCP rows for an allowlisted non-ocx listener", async () => {
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimWithMockClock({
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [100],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [100],
      isAliveFn: () => true,
      verifyOcxFn: () => null,
      killFn: pid => {
        killed.push(pid);
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test("does not drop TCP rows when allowlisted kill throws", async () => {
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [100],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [100],
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: () => {
        throw new Error("kill failed");
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(dropped).toEqual([]);
  });

  test("does not drop TCP rows while allowlisted ocx survives kill", async () => {
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [100],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [100],
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([100]);
    expect(dropped).toEqual([]);
  });

  test("drops TCP rows only after allowlisted ocx is confirmed dead", async () => {
    let alive = true;
    let available = false;
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 200,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [4242],
      isAvailableFn: async () => available,
      listListenPidsFn: () => (alive ? [4242] : []),
      isAliveFn: () => alive,
      verifyOcxFn: pid => pid,
      killFn: () => {
        alive = false;
      },
      dropTcpFn: port => {
        dropped.push(port);
        available = true;
        return { dropped: 2, skippedIpv6: 0, accessDenied: 0 };
      },
      sleepMs: async () => {},
    })).resolves.toBe(true);
    expect(dropped).toEqual([10100]);
  });

  test("killAllOcxOnPort kills ocx listeners absent from the allowlist snapshot", async () => {
    const killed: number[] = [];
    const holderPid = process.pid === 9001 ? 9002 : 9001;
    let holder = holderPid;
    let available = false;
    await expect(reclaimWithMockClock({
      timeoutMs: 200,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: true,
      killAllOcxOnPort: true,
      onlyKillPids: [100], // pre-update PID — the respawned holder is deliberately not self
      isAvailableFn: async () => available,
      listListenPidsFn: () => (holder > 0 ? [holder] : []),
      isAliveFn: pid => pid === holder,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
        if (pid === holder) holder = 0;
      },
      sleepMs: async () => {
        if (holder === 0) available = true;
      },
    })).resolves.toBe(true);
    expect(killed).toEqual([holderPid]);
  });

  test("foreign non-ocx claimant survives reclaim without allowlist or ocx identity", async () => {
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      killAllOcxOnPort: true,
      onlyKillPids: [4242], // trusted old PID — different from the foreign holder
      isAvailableFn: async () => false,
      listListenPidsFn: () => [777],
      isAliveFn: () => true,
      verifyOcxFn: () => null,
      killFn: pid => {
        killed.push(pid);
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test("allowlisted PID that fails ocx verify stays protected until the deadline", async () => {
    const killed: number[] = [];
    const dropped: number[] = [];
    let alive = true;
    let available = false;
    await expect(reclaimWithMockClock({
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [14772],
      isAvailableFn: async () => available,
      // This holder is still alive; a historical PID does not override verifier rejection.
      listListenPidsFn: () => (alive ? [14772] : []),
      isAliveFn: () => alive,
      verifyOcxFn: () => null,
      killFn: pid => {
        killed.push(pid);
        alive = false;
      },
      dropTcpFn: port => {
        dropped.push(port);
        available = true;
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test.each([false, true])("a different verifier PID is rejected with killAllOcxOnPort=%s", async killAllOcxOnPort => {
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimWithMockClock({
      dropTcpRows: true, killOcxHolders: true, killAllOcxOnPort, onlyKillPids: [100],
      isAvailableFn: async () => false, listListenPidsFn: () => [100], isAliveFn: () => true,
      verifyOcxFn: () => 200,
      killFn: pid => { killed.push(pid); },
      dropTcpFn: port => { dropped.push(port); return 1; },
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test("a later successful verification can reclaim a previously rejected holder", async () => {
    let alive = true;
    let available = false;
    let checks = 0;
    const checksAtKill: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimWithMockClock({
      dropTcpRows: true, killOcxHolders: true, onlyKillPids: [100],
      isAvailableFn: async () => available, listListenPidsFn: () => alive ? [100] : [],
      isAliveFn: () => alive,
      verifyOcxFn: pid => ++checks === 1 ? null : pid,
      killFn: () => { checksAtKill.push(checks); alive = false; },
      dropTcpFn: port => { dropped.push(port); available = true; return 1; },
    })).resolves.toBe(true);
    expect(checksAtKill).toEqual([3]); // rejected scan, accepted scan, accepted pre-kill check
    expect(dropped).toEqual([10100]);
  });

  test("dead ghost then same PID reused is killed again under killAllOcxOnPort", async () => {
    const killed: number[] = [];
    let phase: "first-live" | "ghost" | "reuse" = "first-live";
    let available = false;
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 200,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: false,
      killOcxHolders: true,
      killAllOcxOnPort: true,
      onlyKillPids: [],
      isAvailableFn: async () => available,
      listListenPidsFn: () => [4242],
      isAliveFn: () => phase !== "ghost",
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
        if (phase === "first-live") phase = "ghost";
        else if (phase === "reuse") available = true;
      },
      sleepMs: async () => {
        if (phase === "ghost") phase = "reuse";
      },
    })).resolves.toBe(true);
    expect(killed).toEqual([4242, 4242]);
  });

  test("denied authority during the availability await blocks kill and TCP-row drop", async () => {
    let authorized = true;
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [4242],
      // The gate flips while isAvailableFn is in flight — the interleaving a
      // service-home change has against the update job's real reclaimer.
      isAvailableFn: async () => {
        authorized = false;
        return false;
      },
      listListenPidsFn: () => [4242],
      isAliveFn: () => true,
      verifyOcxFn: pid => pid,
      killFn: pid => {
        killed.push(pid);
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
      mayMutateFn: () => authorized,
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([]);
    expect(dropped).toEqual([]);
  });

  test("a mid-wait authority denial vetoes the TCP-row drop after an authorized kill", async () => {
    let authorized = true;
    let alive = true;
    const killed: number[] = [];
    const dropped: number[] = [];
    await expect(reclaimListenPort(10100, "127.0.0.1", {
      timeoutMs: 80,
      intervalMs: 20,
      scanIntervalMs: 20,
      dropTcpRows: true,
      killOcxHolders: true,
      onlyKillPids: [4242],
      isAvailableFn: async () => false,
      listListenPidsFn: () => [4242],
      isAliveFn: () => alive,
      verifyOcxFn: pid => pid,
      // Authority flips while the authorized kill is in flight — the very next
      // destructive step (the TCP-row drop) must already be denied.
      killFn: pid => {
        killed.push(pid);
        alive = false;
        authorized = false;
      },
      dropTcpFn: port => {
        dropped.push(port);
        return { dropped: 1, skippedIpv6: 0, accessDenied: 0 };
      },
      mayMutateFn: () => authorized,
      sleepMs: async () => {},
    })).resolves.toBe(false);
    expect(killed).toEqual([4242]);
    expect(dropped).toEqual([]);
  });
});
