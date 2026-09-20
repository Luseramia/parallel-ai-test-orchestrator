import { readFile } from "node:fs/promises";

export async function assemblePreparePrompt(
  templatePath: string,
  plan: unknown,
  jobId: string,
  baseSha: string,
): Promise<string> {
  const template = await readFile(templatePath, "utf8");
  return `${template.trim()}\n\nJob ID: ${jobId}\nBase SHA: ${baseSha}\n\nTest plan JSON:\n${JSON.stringify(plan, null, 2)}\n`;
}

export async function assembleGeneratePrompt(
  templatePath: string,
  plan: unknown,
  draft: unknown,
  policy: unknown,
  jobId: string,
  baseSha: string,
  codeSha: string,
): Promise<string> {
  const template = await readFile(templatePath, "utf8");
  return `${template.trim()}\n\nJob ID: ${jobId}\nBase SHA: ${baseSha}\nCode SHA: ${codeSha}\n\nTest plan JSON:\n${JSON.stringify(plan, null, 2)}\n\nPrepared draft JSON:\n${JSON.stringify(draft, null, 2)}\n\nEnforced patch policy JSON:\n${JSON.stringify(policy, null, 2)}\n`;
}

