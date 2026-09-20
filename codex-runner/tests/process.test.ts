import assert from "node:assert/strict";
import test from "node:test";

import { ProcessExecutionError, runProcess } from "../src/process.js";

test("runProcess rejects a timed-out child", async () => {
  await assert.rejects(
    runProcess(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
      timeoutMs: 50,
    }),
    (error: unknown) =>
      error instanceof ProcessExecutionError && error.message.includes("timeout"),
  );
});

test("runProcess forwards stdin without invoking a shell", async () => {
  const result = await runProcess(
    process.execPath,
    ["-e", "process.stdin.pipe(process.stdout)"],
    { timeoutMs: 2_000, stdin: "literal;$()" },
  );
  assert.equal(result.stdout, "literal;$()");
});

test("runProcess terminates a child when its signal is aborted", async () => {
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(
    runProcess(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
      timeoutMs: 2_000,
      signal: controller.signal,
    }),
    /aborted/u,
  );
});

