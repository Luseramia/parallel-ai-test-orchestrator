import { mkdtemp, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { runProcess } from "./process.js";

const FULL_SHA = /^[0-9a-f]{40}$/;

export async function checkoutExactSha(
  cloneUrl: string,
  sha: string,
  signal?: AbortSignal,
  preclonedRepository?: string,
): Promise<string> {
  if (!FULL_SHA.test(sha)) throw new Error("checkout requires a full lowercase Git SHA");
  if (preclonedRepository) {
    const repository = await stat(preclonedRepository).catch(() => undefined);
    if (!repository?.isDirectory()) {
      throw new Error("pre-cloned repository is missing");
    }
    const verified = await runProcess(
      "git",
      ["-C", preclonedRepository, "rev-parse", "HEAD"],
      {
        timeoutMs: 10_000,
        maxOutputBytes: 64 * 1024,
        signal,
        env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
      },
    );
    if (verified.stdout.trim() !== sha) {
      throw new Error("pre-cloned repository SHA does not match requested SHA");
    }
    return preclonedRepository;
  }
  const parent = await mkdtemp(join(tmpdir(), "ai-test-codex-"));
  const workspace = join(parent, "repository");
  const processOptions = {
    timeoutMs: 120_000,
    maxOutputBytes: 2 * 1024 * 1024,
    signal,
    env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
  };
  await runProcess("git", ["clone", "--filter=blob:none", "--no-checkout", cloneUrl, workspace], processOptions);
  await runProcess("git", ["-C", workspace, "fetch", "--depth=1", "origin", sha], processOptions);
  await runProcess("git", ["-C", workspace, "checkout", "--detach", sha], processOptions);
  const verified = await runProcess("git", ["-C", workspace, "rev-parse", "HEAD"], processOptions);
  if (verified.stdout.trim() !== sha) throw new Error("checked out Git SHA does not match requested SHA");
  return workspace;
}
