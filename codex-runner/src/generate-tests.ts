import { mkdir, readFile, rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  type ArtifactGateway,
  type ArtifactMetadata,
  downloadArtifact,
  storeArtifact,
} from "./artifacts.js";
import { postCallback } from "./callback.js";
import { runCodex } from "./codex.js";
import { checkoutExactSha } from "./git.js";
import {
  type PatchPolicy,
  PatchPolicyError,
  validateWorkspacePatch,
} from "./patch-policy.js";
import { assembleGeneratePrompt } from "./prompt.js";

export interface GenerateConfiguration {
  jobId: string;
  taskId: string;
  cloneUrl: string;
  baseSha: string;
  codeSha: string;
  planPath: string;
  draftPath: string;
  artifactRoot: string;
  gatewayUrl: string;
  runnerToken: string;
  codexExecutable: string;
  codexArgsPrefix?: readonly string[] | undefined;
  schemaPath: string;
  promptPath: string;
  timeoutMs: number;
  attempt: number;
  patchPolicy: PatchPolicy;
  artifactGateway?: ArtifactGateway;
  planObjectKey?: string;
  draftObjectKey?: string;
}

export async function runGenerate(configuration: GenerateConfiguration): Promise<void> {
  const controller = new AbortController();
  const abort = (): void => controller.abort();
  process.once("SIGTERM", abort);
  process.once("SIGINT", abort);
  const callbackUrl = `${configuration.gatewayUrl.replace(/\/$/u, "")}/internal/v1/test-jobs/${configuration.jobId}/events`;
  let workspace: string | undefined;
  try {
    await sendEvent(configuration, callbackUrl, "GENERATING_TESTS", [], {});
    if (
      configuration.artifactGateway &&
      configuration.planObjectKey &&
      configuration.draftObjectKey
    ) {
      await Promise.all([
        downloadArtifact(
          configuration.artifactGateway,
          configuration.planObjectKey,
          configuration.planPath,
        ),
        downloadArtifact(
          configuration.artifactGateway,
          configuration.draftObjectKey,
          configuration.draftPath,
        ),
      ]);
    }
    const plan = JSON.parse(await readFile(configuration.planPath, "utf8")) as unknown;
    const draft = JSON.parse(await readFile(configuration.draftPath, "utf8")) as unknown;
    workspace = await checkoutExactSha(
      configuration.cloneUrl,
      configuration.codeSha,
      controller.signal,
    );
    const prompt = await assembleGeneratePrompt(
      configuration.promptPath,
      plan,
      draft,
      configuration.patchPolicy,
      configuration.jobId,
      configuration.baseSha,
      configuration.codeSha,
    );
    const outputDirectory = resolve(join(workspace, "..", "runner-output"));
    await mkdir(outputDirectory, { recursive: true });
    const resultPath = join(outputDirectory, "generation-result.json");
    const codex = await runCodex({
      executable: configuration.codexExecutable,
      executableArgsPrefix: configuration.codexArgsPrefix,
      workspace,
      prompt,
      schemaPath: configuration.schemaPath,
      outputPath: resultPath,
      timeoutMs: configuration.timeoutMs,
      sandbox: "workspace-write",
      signal: controller.signal,
    });
    const validatedPatch = await validateWorkspacePatch(
      workspace,
      configuration.codeSha,
      configuration.patchPolicy,
    );
    validateGenerationSemantics(
      plan,
      codex.finalOutput,
      validatedPatch.changedPaths,
    );
    const prefix = `jobs/${configuration.jobId}/attempts/${configuration.attempt}`;
    const artifacts = await Promise.all([
      storeArtifact(
        configuration.artifactRoot,
        `${prefix}/codex-generate.jsonl`,
        Buffer.from(codex.jsonl),
        "CODEX_JSONL",
        "application/x-ndjson",
        configuration.artifactGateway,
      ),
      storeArtifact(
        configuration.artifactRoot,
        `${prefix}/tests.patch`,
        validatedPatch.content,
        "PATCH",
        "text/x-diff",
        configuration.artifactGateway,
      ),
      storeArtifact(
        configuration.artifactRoot,
        `${prefix}/generation-result.json`,
        await readFile(resultPath),
        "RESULT",
        "application/json",
        configuration.artifactGateway,
      ),
    ]);
    await sendEvent(configuration, callbackUrl, "TEST_QUEUED", artifacts, {
      changed_paths: validatedPatch.changedPaths,
    });
  } finally {
    process.removeListener("SIGTERM", abort);
    process.removeListener("SIGINT", abort);
    if (workspace) await rm(resolve(workspace, ".."), { recursive: true, force: true });
  }
}

function validateGenerationSemantics(
  planValue: unknown,
  resultValue: unknown,
  changedPaths: string[],
): void {
  const plan = planValue as { test_cases?: { id: string }[]; acceptance_criteria?: { id: string }[] };
  const result = resultValue as {
    created_tests?: { path: string; test_case_ids: string[]; acceptance_criteria: string[] }[];
    covered_test_cases?: string[];
    uncovered_test_cases?: string[];
  };
  const testCaseIds = new Set((plan.test_cases ?? []).map((item) => item.id));
  const acceptanceIds = new Set((plan.acceptance_criteria ?? []).map((item) => item.id));
  const covered = new Set(result.covered_test_cases ?? []);
  const uncovered = new Set(result.uncovered_test_cases ?? []);
  if ([...covered].some((id) => uncovered.has(id))) {
    throw new PatchPolicyError("INVALID_COVERAGE_MAP", "covered and uncovered test cases overlap");
  }
  const mapped = new Set([...covered, ...uncovered]);
  if (mapped.size !== testCaseIds.size || [...testCaseIds].some((id) => !mapped.has(id))) {
    throw new PatchPolicyError("INVALID_COVERAGE_MAP", "generation result does not account for every test case");
  }
  const createdPaths = new Set((result.created_tests ?? []).map((item) => item.path));
  if (createdPaths.size !== changedPaths.length || changedPaths.some((path) => !createdPaths.has(path))) {
    throw new PatchPolicyError("INVALID_PATH_MAP", "generation result paths do not match the patch");
  }
  for (const created of result.created_tests ?? []) {
    if (created.test_case_ids.some((id) => !testCaseIds.has(id))) {
      throw new PatchPolicyError("UNKNOWN_TEST_CASE", "generation result references an unknown test case");
    }
    if (created.acceptance_criteria.some((id) => !acceptanceIds.has(id))) {
      throw new PatchPolicyError(
        "UNKNOWN_ACCEPTANCE_CRITERION",
        "generation result references an unknown acceptance criterion",
      );
    }
  }
}

async function sendEvent(
  configuration: GenerateConfiguration,
  callbackUrl: string,
  status: "GENERATING_TESTS" | "TEST_QUEUED" | "BLOCKED" | "ERROR",
  artifacts: ArtifactMetadata[],
  summary: object,
): Promise<void> {
  await postCallback({
    url: callbackUrl,
    token: configuration.runnerToken,
    body: {
      schema_version: "1.0",
      event_id: `evt_${configuration.jobId.slice(4)}_generate_${configuration.attempt}_${status.toLowerCase()}`,
      attempt: configuration.attempt,
      phase: "GENERATE",
      status,
      code_sha: configuration.codeSha,
      artifacts,
      summary,
      occurred_at: new Date().toISOString(),
    },
  });
}

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`required environment variable ${name} is not set`);
  return value;
}

export function loadGenerateConfiguration(): GenerateConfiguration {
  const configuration: GenerateConfiguration = {
    jobId: required("JOB_ID"),
    taskId: required("TASK_ID"),
    cloneUrl: required("GIT_CLONE_URL"),
    baseSha: required("BASE_SHA"),
    codeSha: required("CODE_SHA"),
    planPath: required("PLAN_PATH"),
    draftPath: required("DRAFT_PATH"),
    artifactRoot: required("ARTIFACT_ROOT"),
    gatewayUrl: required("GATEWAY_URL"),
    runnerToken: required("RUNNER_TOKEN"),
    codexExecutable: process.env.CODEX_BIN ?? "codex",
    schemaPath:
      process.env.OUTPUT_SCHEMA ?? "/contracts/generation-result.schema.json",
    promptPath: process.env.PROMPT_PATH ?? "/runner/prompts/generate-tests.md",
    timeoutMs: Number(process.env.CODEX_TIMEOUT_MS ?? "900000"),
    attempt: Number(process.env.ATTEMPT ?? "1"),
    patchPolicy: JSON.parse(required("PATCH_POLICY_JSON")) as PatchPolicy,
  };
  const artifactGatewayUrl = process.env.ARTIFACT_GATEWAY_URL;
  if (artifactGatewayUrl) {
    configuration.artifactGateway = {
      baseUrl: artifactGatewayUrl,
      token: required("RUNNER_TOKEN"),
    };
    configuration.planObjectKey = required("PLAN_OBJECT_KEY");
    configuration.draftObjectKey = required("DRAFT_OBJECT_KEY");
  }
  return configuration;
}

async function main(): Promise<void> {
  const configuration = loadGenerateConfiguration();
  try {
    await runGenerate(configuration);
  } catch (error) {
    const callbackUrl = `${configuration.gatewayUrl.replace(/\/$/u, "")}/internal/v1/test-jobs/${configuration.jobId}/events`;
    const blocked = error instanceof PatchPolicyError;
    try {
      await sendEvent(
        configuration,
        callbackUrl,
        blocked ? "BLOCKED" : "ERROR",
        [],
        {
          reason_code: blocked ? error.reasonCode : "AGENT_FAILED",
          error: error instanceof Error ? error.message.slice(0, 500) : "runner failed",
        },
      );
    } catch {
      // The non-zero process exit remains authoritative if callback also fails.
    }
    throw error;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  void main().catch((error: unknown) => {
    process.stderr.write(`${error instanceof Error ? error.message : "runner failed"}\n`);
    process.exitCode = 1;
  });
}
