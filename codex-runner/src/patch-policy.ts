import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";

import { runProcess } from "./process.js";

export interface PatchPolicy {
  allowedPaths: readonly string[];
  deniedPaths: readonly string[];
  allowNewFiles: boolean;
  allowDeletes: boolean;
  allowRenames: boolean;
  allowBinary: boolean;
  allowSymlinks: boolean;
  allowSubmodules: boolean;
  allowModeChanges: boolean;
  maxFiles: number;
  maxBytes: number;
  allowFocusedTests?: boolean;
}

export interface ValidatedPatch {
  content: Buffer;
  changedPaths: string[];
}

export class PatchPolicyError extends Error {
  constructor(
    readonly reasonCode: string,
    message: string,
  ) {
    super(message);
    this.name = "PatchPolicyError";
  }
}

export async function validateWorkspacePatch(
  workspace: string,
  baseSha: string,
  policy: PatchPolicy,
): Promise<ValidatedPatch> {
  const untracked = splitNul(
    (
      await runProcess("git", ["-C", workspace, "ls-files", "--others", "--exclude-standard", "-z"], {
        timeoutMs: 30_000,
      })
    ).stdout,
  );
  if (untracked.length > 0) {
    await runProcess("git", ["-C", workspace, "add", "-N", "--", ...untracked], {
      timeoutMs: 30_000,
    });
  }
  await runProcess("git", ["-C", workspace, "diff", "--check"], {
    timeoutMs: 30_000,
  });
  const nameStatus = (
    await runProcess("git", ["-C", workspace, "diff", "--name-status", "-M", "-z", baseSha], {
      timeoutMs: 30_000,
    })
  ).stdout;
  const changes = parseNameStatus(nameStatus);
  if (changes.length === 0) throw new PatchPolicyError("EMPTY_PATCH", "Codex produced no test changes");
  if (changes.length > policy.maxFiles) {
    throw new PatchPolicyError("TOO_MANY_FILES", `patch changes ${changes.length} files`);
  }
  const paths: string[] = [];
  for (const change of changes) {
    if (change.status.startsWith("R") || change.status.startsWith("C")) {
      if (!policy.allowRenames) throw new PatchPolicyError("RENAME_FORBIDDEN", "renames and copies are forbidden");
    }
    if (change.status === "D" && !policy.allowDeletes) {
      throw new PatchPolicyError("DELETE_FORBIDDEN", `deleting ${change.path} is forbidden`);
    }
    assertSafePath(change.path);
    if (!policy.allowedPaths.some((pattern) => matchesGlob(change.path, pattern))) {
      throw new PatchPolicyError("PATH_NOT_ALLOWED", `${change.path} is outside allowed test paths`);
    }
    if (policy.deniedPaths.some((pattern) => matchesGlob(change.path, pattern))) {
      throw new PatchPolicyError("PATH_DENIED", `${change.path} matches a forbidden path`);
    }
    if (change.status === "A" && !policy.allowNewFiles) {
      throw new PatchPolicyError("NEW_FILE_FORBIDDEN", `adding ${change.path} is forbidden`);
    }
    paths.push(change.path);
  }

  const raw = (
    await runProcess("git", ["-C", workspace, "diff", "--raw", "--no-abbrev", "-z", baseSha], {
      timeoutMs: 30_000,
    })
  ).stdout;
  for (const record of parseRawDiff(raw)) {
    if ((record.oldMode === "120000" || record.newMode === "120000") && !policy.allowSymlinks) {
      throw new PatchPolicyError("SYMLINK_FORBIDDEN", `symlink change at ${record.path} is forbidden`);
    }
    if ((record.oldMode === "160000" || record.newMode === "160000") && !policy.allowSubmodules) {
      throw new PatchPolicyError("SUBMODULE_FORBIDDEN", `submodule change at ${record.path} is forbidden`);
    }
    const isNewOrDeleted = record.oldMode === "000000" || record.newMode === "000000";
    if (!isNewOrDeleted && record.oldMode !== record.newMode && !policy.allowModeChanges) {
      throw new PatchPolicyError("MODE_CHANGE_FORBIDDEN", `mode change at ${record.path} is forbidden`);
    }
  }

  const numstat = (
    await runProcess("git", ["-C", workspace, "diff", "--numstat", "-z", baseSha], {
      timeoutMs: 30_000,
    })
  ).stdout;
  if (!policy.allowBinary && splitNul(numstat).some((entry) => entry.startsWith("-\t-\t"))) {
    throw new PatchPolicyError("BINARY_FORBIDDEN", "binary changes are forbidden");
  }
  if (!policy.allowFocusedTests) {
    const focusedPattern = /(?:\.(?:skip|only)\s*\(|\b(?:xit|xdescribe|fdescribe)\s*\()/u;
    for (const path of paths) {
      if (changes.find((change) => change.path === path)?.status === "D") continue;
      const content = await readFile(join(workspace, path), "utf8");
      if (focusedPattern.test(content)) {
        throw new PatchPolicyError("FOCUSED_TEST_FORBIDDEN", `focused or skipped test found in ${path}`);
      }
    }
  }

  const patchText = (
    await runProcess("git", ["-C", workspace, "diff", "--full-index", "--binary", baseSha], {
      timeoutMs: 30_000,
      maxOutputBytes: policy.maxBytes + 1,
    })
  ).stdout;
  const content = Buffer.from(patchText, "utf8");
  if (content.byteLength > policy.maxBytes) {
    throw new PatchPolicyError("PATCH_TOO_LARGE", `patch exceeds ${policy.maxBytes} bytes`);
  }
  await assertAppliesToExactSha(workspace, baseSha, patchText);
  return { content, changedPaths: [...new Set(paths)].sort() };
}

interface NameStatus {
  status: string;
  path: string;
}

function parseNameStatus(input: string): NameStatus[] {
  const tokens = splitNul(input);
  const result: NameStatus[] = [];
  for (let index = 0; index < tokens.length; ) {
    const status = tokens[index++];
    if (!status) break;
    const path = tokens[index++];
    if (!path) throw new PatchPolicyError("INVALID_DIFF", "Git name-status output is incomplete");
    if (status.startsWith("R") || status.startsWith("C")) {
      const destination = tokens[index++];
      if (!destination) throw new PatchPolicyError("INVALID_DIFF", "Git rename output is incomplete");
      result.push({ status, path: destination });
    } else {
      result.push({ status, path });
    }
  }
  return result;
}

interface RawDiff {
  oldMode: string;
  newMode: string;
  path: string;
}

function parseRawDiff(input: string): RawDiff[] {
  const tokens = splitNul(input);
  const records: RawDiff[] = [];
  for (let index = 0; index < tokens.length; index += 2) {
    const header = tokens[index];
    const path = tokens[index + 1];
    if (!header || !path) throw new PatchPolicyError("INVALID_DIFF", "Git raw diff output is incomplete");
    const fields = header.slice(1).split(" ");
    const oldMode = fields[0];
    const newMode = fields[1];
    if (!oldMode || !newMode) throw new PatchPolicyError("INVALID_DIFF", "Git raw diff modes are missing");
    records.push({ oldMode, newMode, path });
  }
  return records;
}

function splitNul(input: string): string[] {
  return input.split("\0").filter(Boolean);
}

function assertSafePath(path: string): void {
  const parts = path.split("/");
  if (!path || isAbsolute(path) || path.includes("\\") || parts.includes("..") || parts.includes("")) {
    throw new PatchPolicyError("UNSAFE_PATH", "patch contains an unsafe path");
  }
}

export function matchesGlob(path: string, pattern: string): boolean {
  let expression = "^";
  for (let index = 0; index < pattern.length; index += 1) {
    const character = pattern[index];
    if (character === "*" && pattern[index + 1] === "*") {
      if (pattern[index + 2] === "/") {
        expression += "(?:.*/)?";
        index += 2;
      } else {
        expression += ".*";
        index += 1;
      }
    } else if (character === "*") expression += "[^/]*";
    else if (character === "?") expression += "[^/]";
    else expression += character?.replace(/[|\\{}()[\]^$+?.]/gu, "\\$&") ?? "";
  }
  return new RegExp(`${expression}$`, "u").test(path);
}

async function assertAppliesToExactSha(
  workspace: string,
  baseSha: string,
  patch: string,
): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "patch-apply-check-"));
  const checkout = join(directory, "repository");
  try {
    await runProcess("git", ["clone", "--no-local", "--no-checkout", workspace, checkout], {
      timeoutMs: 120_000,
    });
    await runProcess("git", ["-C", checkout, "checkout", "--detach", baseSha], {
      timeoutMs: 30_000,
    });
    await runProcess("git", ["-C", checkout, "apply", "--check", "-"], {
      timeoutMs: 30_000,
      stdin: patch,
    });
  } catch (error) {
    throw new PatchPolicyError("PATCH_APPLY_FAILED", "patch does not apply to the exact code SHA");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

