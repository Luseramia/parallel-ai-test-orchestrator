import { createHash } from "node:crypto";
import { link, mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, normalize, sep } from "node:path";

export interface ArtifactMetadata {
  type: "DRAFT" | "CODEX_JSONL" | "PATCH" | "RESULT";
  object_key: string;
  sha256: string;
  size_bytes: number;
  media_type: string;
}

export interface ArtifactGateway {
  baseUrl: string;
  token: string;
}

export async function storeArtifact(
  root: string,
  objectKey: string,
  content: Buffer,
  type: ArtifactMetadata["type"],
  mediaType: string,
  gateway?: ArtifactGateway,
): Promise<ArtifactMetadata> {
  const normalized = validateObjectKey(objectKey);
  const destination = join(root, normalized);
  await mkdir(dirname(destination), { recursive: true });
  const temporary = `${destination}.tmp-${process.pid}`;
  await writeFile(temporary, content, { flag: "wx" });
  try {
    await link(temporary, destination);
  } catch (error) {
    if (!isAlreadyExists(error)) throw error;
    const existing = await readFile(destination);
    const existingDigest = createHash("sha256").update(existing).digest("hex");
    const incomingDigest = createHash("sha256").update(content).digest("hex");
    if (existingDigest !== incomingDigest) {
      throw new Error("immutable artifact key already contains different data");
    }
  } finally {
    await unlink(temporary).catch(() => undefined);
  }
  const persisted = await readFile(destination);
  const metadata: ArtifactMetadata = {
    type,
    object_key: objectKey.replaceAll("\\", "/"),
    sha256: createHash("sha256").update(persisted).digest("hex"),
    size_bytes: persisted.byteLength,
    media_type: mediaType,
  };
  if (gateway) await uploadArtifact(gateway, objectKey, persisted, metadata);
  return metadata;
}

export async function downloadArtifact(
  gateway: ArtifactGateway,
  objectKey: string,
  destination: string,
): Promise<void> {
  validateObjectKey(objectKey);
  const response = await fetch(artifactUrl(gateway.baseUrl, objectKey), {
    headers: { Authorization: `Bearer ${gateway.token}` },
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`artifact download failed with HTTP ${response.status}`);
  const content = Buffer.from(await response.arrayBuffer());
  await mkdir(dirname(destination), { recursive: true });
  await writeFile(destination, content, { flag: "wx" });
}

async function uploadArtifact(
  gateway: ArtifactGateway,
  objectKey: string,
  content: Buffer,
  metadata: ArtifactMetadata,
): Promise<void> {
  const response = await fetch(artifactUrl(gateway.baseUrl, objectKey), {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${gateway.token}`,
      "Content-Type": "application/octet-stream",
    },
    body: Uint8Array.from(content),
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`artifact upload failed with HTTP ${response.status}`);
  const stored = (await response.json()) as {
    object_key?: string;
    sha256?: string;
    size_bytes?: number;
  };
  if (
    stored.object_key !== metadata.object_key ||
    stored.sha256 !== metadata.sha256 ||
    stored.size_bytes !== metadata.size_bytes
  ) {
    throw new Error("artifact gateway returned mismatched metadata");
  }
}

function artifactUrl(baseUrl: string, objectKey: string): string {
  const encoded = objectKey.split("/").map(encodeURIComponent).join("/");
  return `${baseUrl.replace(/\/$/u, "")}/${encoded}`;
}

function validateObjectKey(objectKey: string): string {
  const normalized = normalize(objectKey);
  if (
    !objectKey ||
    isAbsolute(objectKey) ||
    objectKey.includes("\\") ||
    normalized.startsWith(`..${sep}`) ||
    normalized === ".."
  ) {
    throw new Error("artifact key must be relative to the artifact root");
  }
  return normalized;
}

function isAlreadyExists(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error && error.code === "EEXIST";
}
