import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { AddressInfo } from "node:net";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { runGenerate } from "../src/generate-tests.js";
import type { PatchPolicy } from "../src/patch-policy.js";

const TEST_ROOT = dirname(fileURLToPath(import.meta.url));
const SOURCE_ROOT = resolve(TEST_ROOT, "../..");
const FAKE_CODEX = join(SOURCE_ROOT, "tests", "fixtures", "fake-codex.mjs");
const SCHEMA = resolve(
  SOURCE_ROOT,
  "..",
  "contracts",
  "generation-result.schema.json",
);
const PROMPT = join(SOURCE_ROOT, "prompts", "generate-tests.md");

const POLICY: PatchPolicy = {
  allowedPaths: ["tests/**/*.py"],
  deniedPaths: ["app/**", ".github/**", "package*.json"],
  allowNewFiles: true,
  allowDeletes: false,
  allowRenames: false,
  allowBinary: false,
  allowSymlinks: false,
  allowSubmodules: false,
  allowModeChanges: false,
  maxFiles: 10,
  maxBytes: 100_000,
  allowFocusedTests: false,
};

test("generate runner validates and stores a test-only patch", async () => {
  const directory = await mkdtemp(join(tmpdir(), "generate-runner-test-"));
  const source = join(directory, "source");
  const artifactRoot = join(directory, "artifacts");
  const planPath = join(directory, "plan.json");
  const draftPath = join(directory, "draft.json");
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
    execFileSync("git", ["-C", source, "commit", "-m", "implementation"]);
    const codeSha = execFileSync("git", ["-C", source, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();
    const jobId = `atj_${"2".repeat(32)}`;
    const plan = {
      acceptance_criteria: [{ id: "AC-001" }],
      test_cases: [{ id: "TC-001" }],
    };
    await writeFile(planPath, JSON.stringify(plan));
    await writeFile(draftPath, '{"summary":"draft"}');
    process.env.FAKE_CODEX_WRITE_RELATIVE_PATH = "tests/test_service.py";
    process.env.FAKE_CODEX_WRITE_CONTENT = "def test_service():\n    assert True\n";
    process.env.FAKE_CODEX_OUTPUT = JSON.stringify({
      schema_version: "1.0",
      job_id: jobId,
      task_id: "TASK-123",
      base_sha: codeSha,
      code_sha: codeSha,
      summary: "Generated a focused service test.",
      created_tests: [
        {
          path: "tests/test_service.py",
          test_case_ids: ["TC-001"],
          acceptance_criteria: ["AC-001"],
        },
      ],
      covered_test_cases: ["TC-001"],
      uncovered_test_cases: [],
      risks: [],
    });
    const address = server.address() as AddressInfo;
    await runGenerate({
      jobId,
      taskId: "TASK-123",
      cloneUrl: source,
      baseSha: codeSha,
      codeSha,
      planPath,
      draftPath,
      artifactRoot,
      gatewayUrl: `http://127.0.0.1:${address.port}`,
      runnerToken: "runner-token",
      codexExecutable: process.execPath,
      codexArgsPrefix: [FAKE_CODEX],
      schemaPath: SCHEMA,
      promptPath: PROMPT,
      timeoutMs: 2_000,
      attempt: 1,
      patchPolicy: POLICY,
    });

    assert.equal(callbacks.length, 2);
    assert.equal((callbacks[0] as { status: string }).status, "GENERATING_TESTS");
    const completed = callbacks[1] as {
      status: string;
      artifacts: { type: string; object_key: string }[];
    };
    assert.equal(completed.status, "TEST_QUEUED");
    assert.deepEqual(
      completed.artifacts.map((artifact) => artifact.type).sort(),
      ["CODEX_JSONL", "PATCH", "RESULT"],
    );
    const patch = completed.artifacts.find((artifact) => artifact.type === "PATCH");
    assert.ok(patch);
    assert.match(
      await readFile(join(artifactRoot, patch.object_key), "utf8"),
      /tests\/test_service\.py/u,
    );
  } finally {
    delete process.env.FAKE_CODEX_OUTPUT;
    delete process.env.FAKE_CODEX_WRITE_RELATIVE_PATH;
    delete process.env.FAKE_CODEX_WRITE_CONTENT;
    await new Promise<void>((resolveClose, reject) =>
      server.close((error) => (error ? reject(error) : resolveClose())),
    );
    await rm(directory, { recursive: true, force: true });
  }
});

