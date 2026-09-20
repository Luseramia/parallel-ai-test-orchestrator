import { spawn } from "node:child_process";

export interface ProcessOptions {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  stdin?: string;
  timeoutMs: number;
  maxOutputBytes?: number;
  signal?: AbortSignal | undefined;
}

export interface ProcessResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

export class ProcessExecutionError extends Error {
  constructor(
    message: string,
    readonly result?: ProcessResult,
  ) {
    super(message);
    this.name = "ProcessExecutionError";
  }
}

export async function runProcess(
  executable: string,
  args: readonly string[],
  options: ProcessOptions,
): Promise<ProcessResult> {
  const maxOutputBytes = options.maxOutputBytes ?? 10 * 1024 * 1024;
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [...args], {
      cwd: options.cwd,
      env: options.env,
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let outputBytes = 0;
    let completed = false;

    const terminate = (message: string): void => {
      if (completed) return;
      child.kill("SIGTERM");
      const forceKill = setTimeout(() => child.kill("SIGKILL"), 2_000);
      forceKill.unref();
      reject(new ProcessExecutionError(message));
      completed = true;
    };
    const timeout = setTimeout(
      () => terminate(`process exceeded timeout of ${options.timeoutMs}ms`),
      options.timeoutMs,
    );
    timeout.unref();
    const abort = (): void => terminate("process was aborted");
    options.signal?.addEventListener("abort", abort, { once: true });

    const failOnStreamError = (streamName: string, error: Error): void => {
      if (completed) return;
      completed = true;
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abort);
      reject(new ProcessExecutionError(`${streamName} failed: ${error.message}`));
    };

    const collect = (target: Buffer[], chunk: Buffer): void => {
      outputBytes += chunk.byteLength;
      if (outputBytes > maxOutputBytes) {
        terminate(`process output exceeded ${maxOutputBytes} bytes`);
        return;
      }
      target.push(chunk);
    };
    child.stdout.on("data", (chunk: Buffer) => collect(stdout, chunk));
    child.stderr.on("data", (chunk: Buffer) => collect(stderr, chunk));
    child.stdin.on("error", (error: NodeJS.ErrnoException) => {
      // A short-lived child may close stdin before the prompt has finished
      // writing. The child exit status remains authoritative in that case.
      if (error.code !== "EPIPE") failOnStreamError("stdin", error);
    });
    child.stdout.on("error", (error) => failOnStreamError("stdout", error));
    child.stderr.on("error", (error) => failOnStreamError("stderr", error));
    child.once("error", (error) => {
      if (!completed) {
        completed = true;
        clearTimeout(timeout);
        reject(new ProcessExecutionError(`could not start process: ${error.message}`));
      }
    });
    child.once("close", (code) => {
      if (completed) return;
      completed = true;
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abort);
      const result: ProcessResult = {
        exitCode: code ?? -1,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      };
      if (result.exitCode !== 0) {
        reject(
          new ProcessExecutionError(
            `process exited with code ${result.exitCode}`,
            result,
          ),
        );
      } else {
        resolve(result);
      }
    });
    child.stdin.end(options.stdin ?? "");
  });
}
