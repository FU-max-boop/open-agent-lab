import { Buffer } from "node:buffer";
import { randomBytes } from "node:crypto";
import { lstat, mkdir, mkdtemp, open, rename, rm } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";

import {
  EVIDENCE_MANIFEST_VERSION,
  canonicalJson,
  type EvidenceFileV1,
  type EvidenceManifestBodyV1,
  type EvidenceManifestV1,
  type JsonObject,
} from "@open-agent-lab/contracts";

import { manifestIdFor, sha256 } from "./digest.js";
import { EvidenceError, isNodeError } from "./errors.js";
import {
  resolveEvidenceLimits,
  type EvidenceLimitOptions,
} from "./limits.js";
import { assertSafeEvidencePath, portablePathKey } from "./path.js";

export interface EvidenceFileInput {
  path: string;
  content: string | Uint8Array;
  mediaType?: string;
  role?: string;
}

export interface EvidenceBundleInput {
  runId: string;
  createdAt?: string;
  files: readonly EvidenceFileInput[];
  metadata?: JsonObject;
}

export type WriteEvidenceBundleOptions = EvidenceLimitOptions;

interface PreparedFile {
  descriptor: EvidenceFileV1;
  content: Buffer;
}

function safeLabel(value: unknown, name: string, maxLength: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maxLength ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    throw new EvidenceError("INVALID_INPUT", `${name} must be a non-empty safe string`);
  }
  return value;
}

function canonicalTimestamp(value: unknown): string {
  if (typeof value !== "string") {
    throw new EvidenceError("INVALID_INPUT", "createdAt must be a string");
  }
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.valueOf()) || parsed.toISOString() !== value) {
    throw new EvidenceError(
      "INVALID_INPUT",
      "createdAt must use canonical UTC ISO-8601 form",
    );
  }
  return value;
}

function cloneMetadata(value: unknown): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new EvidenceError("INVALID_INPUT", "metadata must be a JSON object");
  }
  try {
    return JSON.parse(canonicalJson(value)) as JsonObject;
  } catch (error) {
    throw new EvidenceError("INVALID_INPUT", "metadata must contain only strict JSON values", {
      cause: error,
    });
  }
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return false;
    throw error;
  }
}

async function durableExclusiveWrite(path: string, content: Uint8Array): Promise<void> {
  const handle = await open(path, "wx", 0o600);
  try {
    await handle.writeFile(content);
    await handle.sync();
  } finally {
    await handle.close();
  }
}

function prepareFiles(
  input: EvidenceBundleInput,
  limits: ReturnType<typeof resolveEvidenceLimits>,
): PreparedFile[] {
  if (!Array.isArray(input.files)) {
    throw new EvidenceError("INVALID_INPUT", "files must be an array");
  }
  if (input.files.length > limits.maxFiles) {
    throw new EvidenceError(
      "LIMIT_EXCEEDED",
      `Bundle has ${input.files.length} files; maximum is ${limits.maxFiles}`,
    );
  }

  const seen = new Set<string>();
  const prepared: PreparedFile[] = [];
  let totalBytes = 0;
  for (const file of input.files) {
    if (typeof file !== "object" || file === null) {
      throw new EvidenceError("INVALID_INPUT", "Every file must be an object");
    }
    assertSafeEvidencePath(file.path);
    const key = portablePathKey(file.path);
    if (seen.has(key)) {
      throw new EvidenceError("DUPLICATE_PATH", `Duplicate evidence path: ${file.path}`);
    }
    seen.add(key);

    if (typeof file.content !== "string" && !(file.content instanceof Uint8Array)) {
      throw new EvidenceError(
        "INVALID_INPUT",
        `Content for ${file.path} must be a string or Uint8Array`,
      );
    }
    const content =
      typeof file.content === "string"
        ? Buffer.from(file.content, "utf8")
        : Buffer.from(file.content);
    if (content.byteLength > limits.maxFileBytes) {
      throw new EvidenceError(
        "LIMIT_EXCEEDED",
        `${file.path} exceeds maxFileBytes (${limits.maxFileBytes})`,
      );
    }
    totalBytes += content.byteLength;
    if (!Number.isSafeInteger(totalBytes) || totalBytes > limits.maxTotalBytes) {
      throw new EvidenceError(
        "LIMIT_EXCEEDED",
        `Bundle exceeds maxTotalBytes (${limits.maxTotalBytes})`,
      );
    }

    const descriptor: EvidenceFileV1 = {
      path: file.path,
      size: content.byteLength,
      sha256: sha256(content),
      mediaType:
        file.mediaType === undefined
          ? "application/octet-stream"
          : safeLabel(file.mediaType, `mediaType for ${file.path}`, 255),
    };
    if (file.role !== undefined) {
      descriptor.role = safeLabel(file.role, `role for ${file.path}`, 128);
    }
    prepared.push({ descriptor, content });
  }

  prepared.sort((left, right) =>
    left.descriptor.path < right.descriptor.path
      ? -1
      : left.descriptor.path > right.descriptor.path
        ? 1
        : 0,
  );
  return prepared;
}

/**
 * Creates a new evidence directory with atomic publication semantics.
 *
 * The target is never overwritten. Files are synced into a sibling staging
 * directory and the fully formed directory is exposed with one rename.
 */
export async function writeEvidenceBundle(
  targetDirectory: string,
  input: EvidenceBundleInput,
  options?: WriteEvidenceBundleOptions,
): Promise<EvidenceManifestV1> {
  if (typeof targetDirectory !== "string" || targetDirectory.length === 0) {
    throw new EvidenceError("INVALID_INPUT", "targetDirectory must be non-empty");
  }
  if (typeof input !== "object" || input === null) {
    throw new EvidenceError("INVALID_INPUT", "Evidence bundle input must be an object");
  }

  const limits = resolveEvidenceLimits(options);
  const runId = safeLabel(input.runId, "runId", 256);
  const createdAt = canonicalTimestamp(input.createdAt ?? new Date().toISOString());
  const files = prepareFiles(input, limits);
  const body: EvidenceManifestBodyV1 = {
    schemaVersion: EVIDENCE_MANIFEST_VERSION,
    runId,
    createdAt,
    files: files.map(({ descriptor }) => descriptor),
  };
  if (input.metadata !== undefined) body.metadata = cloneMetadata(input.metadata);

  const manifest: EvidenceManifestV1 = {
    ...body,
    manifestId: manifestIdFor(body),
  };
  const manifestBytes = Buffer.from(canonicalJson(manifest), "utf8");
  if (manifestBytes.byteLength > limits.maxManifestBytes) {
    throw new EvidenceError(
      "LIMIT_EXCEEDED",
      `manifest.json exceeds maxManifestBytes (${limits.maxManifestBytes})`,
    );
  }

  const target = resolve(targetDirectory);
  const parent = dirname(target);
  const targetName = basename(target);
  await mkdir(parent, { recursive: true });

  let staging: string | undefined;
  try {
    if (await pathExists(target)) {
      throw new EvidenceError("TARGET_EXISTS", `Evidence target already exists: ${target}`);
    }
    const randomSuffix = randomBytes(6).toString("hex");
    staging = await mkdtemp(join(parent, `.${targetName}.tmp-${randomSuffix}-`));

    for (const file of files) {
      const destination = join(staging, ...file.descriptor.path.split("/"));
      await mkdir(dirname(destination), { recursive: true, mode: 0o700 });
      await durableExclusiveWrite(destination, file.content);
    }
    await durableExclusiveWrite(join(staging, "manifest.json"), manifestBytes);

    // Every bundle contains manifest.json, so a racing directory publication is
    // non-empty and cannot be replaced by POSIX rename. The explicit second
    // check also gives a useful error before attempting the atomic operation.
    if (await pathExists(target)) {
      throw new EvidenceError("TARGET_EXISTS", `Evidence target appeared during write: ${target}`);
    }
    try {
      await rename(staging, target);
    } catch (error) {
      if (
        isNodeError(error) &&
        (error.code === "EEXIST" ||
          error.code === "ENOTEMPTY" ||
          error.code === "EISDIR")
      ) {
        throw new EvidenceError("TARGET_EXISTS", `Evidence target already exists: ${target}`, {
          cause: error,
        });
      }
      throw error;
    }
    staging = undefined;
    return manifest;
  } catch (error) {
    if (error instanceof EvidenceError) throw error;
    throw new EvidenceError("IO_ERROR", `Failed to write evidence bundle ${target}`, {
      cause: error,
    });
  } finally {
    if (staging !== undefined) {
      await rm(staging, { recursive: true, force: true }).catch(() => undefined);
    }
  }
}
