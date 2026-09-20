import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

const mode = process.env.FAKE_CODEX_MODE ?? "valid";
if (mode === "timeout") {
  setTimeout(() => {}, 60_000);
} else {
  const outputFlag = process.argv.indexOf("-o");
  if (outputFlag < 0 || !process.argv[outputFlag + 1]) {
    process.stderr.write("missing -o output path\n");
    process.exitCode = 2;
  } else {
    for await (const _chunk of process.stdin) {
      // Consume the complete prompt before producing output.
    }
    const writePath =
      process.env.FAKE_CODEX_WRITE_PATH ??
      (process.env.FAKE_CODEX_WRITE_RELATIVE_PATH
        ? join(process.cwd(), process.env.FAKE_CODEX_WRITE_RELATIVE_PATH)
        : undefined);
    if (writePath) {
      await mkdir(dirname(writePath), { recursive: true });
      await writeFile(
        writePath,
        process.env.FAKE_CODEX_WRITE_CONTENT ?? "generated test\n",
      );
    }
    const output =
      mode === "invalid-output"
        ? {}
        : JSON.parse(process.env.FAKE_CODEX_OUTPUT ?? "{}");
    await writeFile(process.argv[outputFlag + 1], JSON.stringify(output));
    if (mode === "invalid-jsonl") {
      process.stdout.write("not-json\n");
    } else {
      process.stdout.write('{"type":"thread.started","thread_id":"fake-thread"}\n');
      process.stdout.write('{"type":"item.completed"}\n');
    }
  }
}
