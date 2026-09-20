import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { checkoutExactSha } from "../src/git.js";

test("checkoutExactSha checks out and verifies the requested commit", async () => {
  const directory = await mkdtemp(join(tmpdir(), "codex-git-test-"));
  const source = join(directory, "source");
  try {
    execFileSync("git", ["init", source]);
    execFileSync("git", ["-C", source, "config", "user.email", "tests@example.test"]);
    execFileSync("git", ["-C", source, "config", "user.name", "Runner Tests"]);
    await writeFile(join(source, "value.txt"), "first");
    execFileSync("git", ["-C", source, "add", "value.txt"]);
    execFileSync("git", ["-C", source, "commit", "-m", "first"]);
    const sha = execFileSync("git", ["-C", source, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();
    await writeFile(join(source, "value.txt"), "second");
    execFileSync("git", ["-C", source, "commit", "-am", "second"]);

    const checkout = await checkoutExactSha(source, sha);
    assert.equal(await readFile(join(checkout, "value.txt"), "utf8"), "first");
    assert.equal(
      execFileSync("git", ["-C", checkout, "rev-parse", "HEAD"], {
        encoding: "utf8",
      }).trim(),
      sha,
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("checkoutExactSha rejects abbreviated SHAs", async () => {
  await assert.rejects(
    checkoutExactSha("unused", "abc123"),
    /full lowercase Git SHA/u,
  );
});

