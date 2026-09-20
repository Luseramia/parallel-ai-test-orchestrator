import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  type PatchPolicy,
  PatchPolicyError,
  matchesGlob,
  validateWorkspacePatch,
} from "../src/patch-policy.js";

const POLICY: PatchPolicy = {
  allowedPaths: ["tests/**/*.py", "tests/fixtures/**"],
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

test("glob matching supports recursive test patterns", () => {
  assert.equal(matchesGlob("tests/test_one.py", "tests/**/*.py"), true);
  assert.equal(matchesGlob("tests/unit/test_one.py", "tests/**/*.py"), true);
  assert.equal(matchesGlob("app/test_one.py", "tests/**/*.py"), false);
});

test("validator accepts a new allowlisted text test and returns an applicable patch", async () => {
  await withRepository(async (workspace, baseSha) => {
    await writeFile(join(workspace, "tests", "test_new.py"), "def test_new():\n    assert True\n");
    const patch = await validateWorkspacePatch(workspace, baseSha, POLICY);
    assert.deepEqual(patch.changedPaths, ["tests/test_new.py"]);
    assert.match(patch.content.toString("utf8"), /def test_new/u);
  });
});

test("validator rejects production paths", async () => {
  await withRepository(async (workspace, baseSha) => {
    await writeFile(join(workspace, "app", "service.py"), "VALUE = 2\n");
    await assertPolicyError(
      validateWorkspacePatch(workspace, baseSha, POLICY),
      "PATH_NOT_ALLOWED",
    );
  });
});

test("validator rejects focused or skipped tests", async () => {
  await withRepository(async (workspace, baseSha) => {
    await writeFile(join(workspace, "tests", "test_new.py"), "describe.skip('x', () => {})\n");
    await assertPolicyError(
      validateWorkspacePatch(workspace, baseSha, POLICY),
      "FOCUSED_TEST_FORBIDDEN",
    );
  });
});

test("validator rejects deletion and rename", async (context) => {
  await context.test("delete", async () => {
    await withRepository(async (workspace, baseSha) => {
      execFileSync("git", ["-C", workspace, "rm", "tests/test_existing.py"]);
      await assertPolicyError(
        validateWorkspacePatch(workspace, baseSha, POLICY),
        "DELETE_FORBIDDEN",
      );
    });
  });
  await context.test("rename", async () => {
    await withRepository(async (workspace, baseSha) => {
      execFileSync("git", [
        "-C",
        workspace,
        "mv",
        "tests/test_existing.py",
        "tests/test_renamed.py",
      ]);
      await assertPolicyError(
        validateWorkspacePatch(workspace, baseSha, POLICY),
        "RENAME_FORBIDDEN",
      );
    });
  });
});

test("validator rejects binary changes", async () => {
  await withRepository(async (workspace, baseSha) => {
    await writeFile(
      join(workspace, "tests", "fixtures", "payload.bin"),
      Buffer.from([0, 1, 2, 3, 0, 255]),
    );
    await assertPolicyError(
      validateWorkspacePatch(workspace, baseSha, POLICY),
      "BINARY_FORBIDDEN",
    );
  });
});

async function withRepository(
  callback: (workspace: string, baseSha: string) => Promise<void>,
): Promise<void> {
  const workspace = await mkdtemp(join(tmpdir(), "patch-policy-test-"));
  try {
    execFileSync("git", ["init", workspace]);
    execFileSync("git", ["-C", workspace, "config", "user.email", "tests@example.test"]);
    execFileSync("git", ["-C", workspace, "config", "user.name", "Patch Tests"]);
    await mkdir(join(workspace, "app"), { recursive: true });
    await mkdir(join(workspace, "tests", "fixtures"), { recursive: true });
    await writeFile(join(workspace, "app", "service.py"), "VALUE = 1\n");
    await writeFile(join(workspace, "tests", "test_existing.py"), "def test_existing():\n    assert True\n");
    execFileSync("git", ["-C", workspace, "add", "."]);
    execFileSync("git", ["-C", workspace, "commit", "-m", "base"]);
    const baseSha = execFileSync("git", ["-C", workspace, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();
    await callback(workspace, baseSha);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
}

async function assertPolicyError(
  promise: Promise<unknown>,
  reasonCode: string,
): Promise<void> {
  await assert.rejects(
    promise,
    (error: unknown) =>
      error instanceof PatchPolicyError && error.reasonCode === reasonCode,
  );
}

