import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { AddressInfo } from "node:net";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { runPrepare } from "../src/prepare.js";

const TEST_ROOT = dirname(fileURLToPath(import.meta.url));
const SOURCE_ROOT = resolve(TEST_ROOT, "../..");
const FAKE_CODEX = join(SOURCE_ROOT, "tests", "fixtures", "fake-codex.mjs");
const SCHEMA = resolve(SOURCE_ROOT, "..", "contracts", "test-draft.schema.json");
const PROMPT = join(SOURCE_ROOT, "prompts", "prepare.md");

test("prepare runner checks out exact SHA, stores artifacts, and sends callbacks", async () => {
  const directory = await mkdtemp(join(tmpdir(), "prepare-runner-test-"));
  const source = join(directory, "source");
  const artifactRoot = join(directory, "artifacts");
  const planPath = join(directory, "plan.json");
  const callbacks: unknown[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      callbacks.push(JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown);
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"ok":true}');
    });
  });
  await new Promise<void>((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  try {
    execFileSync("git", ["init", source]);
    execFileSync("git", ["-C", source, "config", "user.email", "tests@example.test"]);
    execFileSync("git", ["-C", source, "config", "user.name", "Runner Tests"]);
    await writeFile(join(source, "service.py"), "VALUE = 1\n");
    execFileSync("git", ["-C", source, "add", "service.py"]);
    execFileSync("git", ["-C", source, "commit", "-m", "base"]);
    const baseSha = execFileSync("git", ["-C", source, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();
    await writeFile(planPath, '{"schema_version":"1.0"}');

    const jobId = `atj_${"1".repeat(32)}`;
    process.env.FAKE_CODEX_OUTPUT = JSON.stringify({
      schema_version: "1.0",
      job_id: jobId,
      task_id: "TASK-123",
      base_sha: baseSha,
      summary: "Prepared against the exact base SHA.",
      proposed_tests: [
        {
          test_case_id: "TC-001",
          acceptance_criteria: ["AC-001"],
          level: "unit",
          path: "tests/test_service.py",
          description: "Covers the planned behavior.",
        },
      ],
      risks: [],
      command_ids: ["python-unittest-all"],
    });
    const address = server.address() as AddressInfo;
    await runPrepare({
      jobId,
      taskId: "TASK-123",
      cloneUrl: source,
      baseSha,
      planPath,
      artifactRoot,
      gatewayUrl: `http://127.0.0.1:${address.port}`,
      runnerToken: "runner-token",
      codexExecutable: process.execPath,
      codexArgsPrefix: [FAKE_CODEX],
      schemaPath: SCHEMA,
      promptPath: PROMPT,
      timeoutMs: 2_000,
      attempt: 1,
    });

    assert.equal(callbacks.length, 2);
    assert.equal((callbacks[0] as { status: string }).status, "PREPARING");
    const completed = callbacks[1] as { status: string; artifacts: { object_key: string }[] };
    assert.equal(completed.status, "WAITING_FOR_CODE");
    assert.equal(completed.artifacts.length, 2);
    for (const artifact of completed.artifacts) {
      assert.ok((await readFile(join(artifactRoot, artifact.object_key))).byteLength > 0);
    }
  } finally {
    delete process.env.FAKE_CODEX_OUTPUT;
    await new Promise<void>((resolveClose, reject) =>
      server.close((error) => (error ? reject(error) : resolveClose())),
    );
    await rm(directory, { recursive: true, force: true });
  }
});

