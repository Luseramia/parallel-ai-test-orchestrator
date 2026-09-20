import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { parseJsonLines, runCodex } from "../src/codex.js";

const TEST_ROOT = dirname(fileURLToPath(import.meta.url));
const SOURCE_ROOT = resolve(TEST_ROOT, "../..");
const FAKE_CODEX = join(SOURCE_ROOT, "tests", "fixtures", "fake-codex.mjs");
const SCHEMA = resolve(SOURCE_ROOT, "..", "contracts", "test-draft.schema.json");

function validOutput(): object {
  return {
    schema_version: "1.0",
    job_id: `atj_${"1".repeat(32)}`,
    task_id: "TASK-123",
    base_sha: "a".repeat(40),
    summary: "A focused test draft.",
    proposed_tests: [
      {
        test_case_id: "TC-001",
        acceptance_criteria: ["AC-001"],
        level: "unit",
        path: "tests/test_service.py",
        description: "Covers the service contract.",
      },
    ],
    risks: [],
    command_ids: ["python-unittest-all"],
  };
}

test("runCodex accepts JSONL and schema-valid final output", async () => {
  const directory = await mkdtemp(join(tmpdir(), "codex-runner-test-"));
  try {
    const result = await runCodex({
      executable: process.execPath,
      executableArgsPrefix: [FAKE_CODEX],
      workspace: directory,
      prompt: "prepare",
      schemaPath: SCHEMA,
      outputPath: join(directory, "output.json"),
      timeoutMs: 2_000,
      sandbox: "read-only",
    });
    assert.deepEqual(result.finalOutput, validOutput());
    assert.equal(parseJsonLines(result.jsonl).length, 2);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("runCodex rejects invalid JSONL", async () => {
  const directory = await mkdtemp(join(tmpdir(), "codex-runner-test-"));
  try {
    process.env.FAKE_CODEX_MODE = "invalid-jsonl";
    await assert.rejects(
      runCodex({
        executable: process.execPath,
        executableArgsPrefix: [FAKE_CODEX],
        workspace: directory,
        prompt: "prepare",
        schemaPath: SCHEMA,
        outputPath: join(directory, "output.json"),
        timeoutMs: 2_000,
        sandbox: "read-only",
      }),
      /invalid JSON/u,
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("runCodex rejects schema-invalid final output", async () => {
  const directory = await mkdtemp(join(tmpdir(), "codex-runner-test-"));
  try {
    process.env.FAKE_CODEX_MODE = "invalid-output";
    await assert.rejects(
      runCodex({
        executable: process.execPath,
        executableArgsPrefix: [FAKE_CODEX],
        workspace: directory,
        prompt: "prepare",
        schemaPath: SCHEMA,
        outputPath: join(directory, "output.json"),
        timeoutMs: 2_000,
        sandbox: "read-only",
      }),
      /schema validation/u,
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test.beforeEach(() => {
  process.env.FAKE_CODEX_OUTPUT = JSON.stringify(validOutput());
  delete process.env.FAKE_CODEX_MODE;
});

test.afterEach(() => {
  delete process.env.FAKE_CODEX_OUTPUT;
  delete process.env.FAKE_CODEX_MODE;
});
