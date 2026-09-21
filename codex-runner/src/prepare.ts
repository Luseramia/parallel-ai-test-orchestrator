import { mkdir, readFile, rm } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { downloadArtifact, storeArtifact, type ArtifactGateway } from "./artifacts.js";
import { postCallback } from "./callback.js";
import { runCodex } from "./codex.js";
import { checkoutExactSha } from "./git.js";
import { assemblePreparePrompt } from "./prompt.js";

export interface Configuration {
  jobId: string;
  taskId: string;
  cloneUrl: string;
  baseSha: string;
  planPath: string;
  artifactRoot: string;
  gatewayUrl: string;
  runnerToken: string;
  codexExecutable: string;
  codexArgsPrefix?: readonly string[] | undefined;
  schemaPath: string;
  promptPath: string;
  timeoutMs: number;
  attempt: number;
  preclonedRepository?: string;
  artifactGateway?: ArtifactGateway;
  planObjectKey?: string;
}

export async function runPrepare(configuration: Configuration): Promise<void> {
  const controller = new AbortController();
  const abort = (): void => controller.abort();
  process.once("SIGTERM", abort);
  process.once("SIGINT", abort);
  const callbackUrl = `${configuration.gatewayUrl.replace(/\/$/u, "")}/internal/v1/test-jobs/${configuration.jobId}/events`;
  let workspace: string | undefined;
  try {
    await postCallback({
      url: callbackUrl,
      token: configuration.runnerToken,
      body: event(configuration, "PREPARING", [], {}),
    });
    if (configuration.artifactGateway && configuration.planObjectKey) {
      await downloadArtifact(
        configuration.artifactGateway,
        configuration.planObjectKey,
        configuration.planPath,
      );
    }
    const plan = JSON.parse(await readFile(configuration.planPath, "utf8")) as unknown;
    workspace = await checkoutExactSha(
      configuration.cloneUrl,
      configuration.baseSha,
      controller.signal,
      configuration.preclonedRepository,
    );
    const prompt = await assemblePreparePrompt(
      configuration.promptPath,
      plan,
      configuration.jobId,
      configuration.baseSha,
    );
    const outputDirectory = resolve(join(workspace, "..", "runner-output"));
    await mkdir(outputDirectory, { recursive: true });
    const finalOutputPath = join(outputDirectory, "test-draft.json");
    const codex = await runCodex({
      executable: configuration.codexExecutable,
      executableArgsPrefix: configuration.codexArgsPrefix,
      workspace,
      prompt,
      schemaPath: configuration.schemaPath,
      outputPath: finalOutputPath,
      timeoutMs: configuration.timeoutMs,
      sandbox: "read-only",
      signal: controller.signal,
    });
    const prefix = `jobs/${configuration.jobId}/attempts/${configuration.attempt}`;
    const jsonlArtifact = await storeArtifact(
      configuration.artifactRoot,
      `${prefix}/codex-prepare.jsonl`,
      Buffer.from(codex.jsonl),
      "CODEX_JSONL",
      "application/x-ndjson",
      configuration.artifactGateway,
    );
    const draftArtifact = await storeArtifact(
      configuration.artifactRoot,
      `${prefix}/test-draft.json`,
      await readFile(finalOutputPath),
      "DRAFT",
      "application/json",
      configuration.artifactGateway,
    );
    await postCallback({
      url: callbackUrl,
      token: configuration.runnerToken,
      body: event(
        configuration,
        "WAITING_FOR_CODE",
        [jsonlArtifact, draftArtifact],
        { prepared: true },
      ),
    });
  } finally {
    process.removeListener("SIGTERM", abort);
    process.removeListener("SIGINT", abort);
    if (workspace && !configuration.preclonedRepository) {
      await rm(resolve(workspace, ".."), { recursive: true, force: true });
    }
  }
}

function event(
  configuration: Configuration,
  status: "PREPARING" | "WAITING_FOR_CODE" | "ERROR",
  artifacts: object[],
  summary: object,
): object {
  const suffix = status.toLowerCase();
  const artifactGatewayUrl = process.env.ARTIFACT_GATEWAY_URL;
  return {
    schema_version: "1.0",
    event_id: `evt_${configuration.jobId.slice(4)}_prepare_${configuration.attempt}_${suffix}`,
    attempt: configuration.attempt,
    phase: "PREPARE",
    status,
    code_sha: null,
    artifacts,
    summary,
    occurred_at: new Date().toISOString(),
  };
}

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`required environment variable ${name} is not set`);
  return value;
}

function loadConfiguration(): Configuration {
  const configuration: Configuration = {
    jobId: required("JOB_ID"),
    taskId: required("TASK_ID"),
    cloneUrl: required("GIT_CLONE_URL"),
    baseSha: required("BASE_SHA"),
    planPath: required("PLAN_PATH"),
    artifactRoot: required("ARTIFACT_ROOT"),
    gatewayUrl: required("GATEWAY_URL"),
    runnerToken: required("RUNNER_TOKEN"),
    codexExecutable: process.env.CODEX_BIN ?? "codex",
    schemaPath: process.env.OUTPUT_SCHEMA ?? "/contracts/test-draft.schema.json",
    promptPath: process.env.PROMPT_PATH ?? "/runner/prompts/prepare.md",
    timeoutMs: Number(process.env.CODEX_TIMEOUT_MS ?? "900000"),
    attempt: Number(process.env.ATTEMPT ?? "1"),
  };
  const artifactGatewayUrl = process.env.ARTIFACT_GATEWAY_URL;
  const preclonedRepository = process.env.PRECLONED_REPOSITORY;
  if (preclonedRepository) configuration.preclonedRepository = preclonedRepository;
  if (artifactGatewayUrl) {
    configuration.artifactGateway = {
      baseUrl: artifactGatewayUrl,
      token: required("RUNNER_TOKEN"),
    };
    configuration.planObjectKey = required("PLAN_OBJECT_KEY");
  }
  return configuration;
}

async function main(): Promise<void> {
  const configuration = loadConfiguration();
  try {
    await runPrepare(configuration);
  } catch (error) {
    const callbackUrl = `${configuration.gatewayUrl.replace(/\/$/u, "")}/internal/v1/test-jobs/${configuration.jobId}/events`;
    const message = error instanceof Error ? error.message : "unknown runner error";
    try {
      await postCallback({
        url: callbackUrl,
        token: configuration.runnerToken,
        body: event(configuration, "ERROR", [], { error: message.slice(0, 500) }),
      });
    } catch {
      // The process exit remains authoritative when the failure callback also fails.
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

export { loadConfiguration };
