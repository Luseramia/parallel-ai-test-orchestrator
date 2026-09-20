import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";

import type { Ajv2020 as Ajv2020Class, ErrorObject } from "ajv/dist/2020.js";
import type { FormatsPlugin } from "ajv-formats";

import { runProcess } from "./process.js";

const require = createRequire(import.meta.url);
const Ajv2020 = require("ajv/dist/2020.js") as typeof Ajv2020Class;
const addFormats = require("ajv-formats") as FormatsPlugin;

export interface CodexResult {
  jsonl: string;
  finalOutput: unknown;
}

export interface CodexOptions {
  executable: string;
  executableArgsPrefix?: readonly string[] | undefined;
  workspace: string;
  prompt: string;
  schemaPath: string;
  outputPath: string;
  timeoutMs: number;
  sandbox: "read-only" | "workspace-write";
  signal?: AbortSignal | undefined;
}

export async function runCodex(options: CodexOptions): Promise<CodexResult> {
  const result = await runProcess(
    options.executable,
    [
      ...(options.executableArgsPrefix ?? []),
      "exec",
      "--ephemeral",
      "--sandbox",
      options.sandbox,
      "--json",
      "--output-schema",
      options.schemaPath,
      "-o",
      options.outputPath,
      "-",
    ],
    {
      cwd: options.workspace,
      stdin: options.prompt,
      timeoutMs: options.timeoutMs,
      maxOutputBytes: 20 * 1024 * 1024,
      signal: options.signal,
      env: process.env,
    },
  );
  parseJsonLines(result.stdout);
  const finalOutput = JSON.parse(await readFile(options.outputPath, "utf8")) as unknown;
  await validateJsonSchema(options.schemaPath, finalOutput);
  return { jsonl: result.stdout, finalOutput };
}

export function parseJsonLines(input: string): unknown[] {
  const lines = input.split(/\r?\n/u).filter((line) => line.trim());
  if (lines.length === 0) throw new Error("Codex produced no JSONL events");
  return lines.map((line, index) => {
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      throw new Error(`Codex JSONL line ${index + 1} is invalid JSON`);
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`Codex JSONL line ${index + 1} is not an object`);
    }
    return value;
  });
}

export async function validateJsonSchema(schemaPath: string, value: unknown): Promise<void> {
  const schema = JSON.parse(await readFile(schemaPath, "utf8")) as object;
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  addFormats(ajv);
  const validate = ajv.compile(schema);
  if (!validate(value)) throw new Error(formatErrors(validate.errors));
}

function formatErrors(errors: ErrorObject[] | null | undefined): string {
  return `structured output failed schema validation: ${(errors ?? [])
    .map((error) => `${error.instancePath || "$"} ${error.message ?? "is invalid"}`)
    .join("; ")}`;
}
