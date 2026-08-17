import {
  EVIDENCE_MANIFEST_VERSION,
  canonicalJson,
  type EvidenceFileV1,
  type EvidenceManifestBodyV1,
  type EvidenceManifestV1,
  type JsonObject,
} from "@open-agent-lab/contracts";

import { isSha256Digest, manifestIdFor } from "./digest.js";
import { EvidenceError } from "./errors.js";
import { assertSafeEvidencePath, portablePathKey } from "./path.js";

function isObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertExactKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[],
  at: string,
): void {
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new EvidenceError("INVALID_MANIFEST", `Unknown key ${at}.${key}`);
    }
  }
  for (const key of required) {
    if (!Object.hasOwn(value, key)) {
      throw new EvidenceError("INVALID_MANIFEST", `Missing key ${at}.${key}`);
    }
  }
}

function assertNonEmptyString(value: unknown, at: string, maximum = 1_024): asserts value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    throw new EvidenceError("INVALID_MANIFEST", `${at} must be a non-empty safe string`);
  }
}

function assertCanonicalTimestamp(value: unknown, at: string): asserts value is string {
  if (typeof value !== "string") {
    throw new EvidenceError("INVALID_MANIFEST", `${at} must be an ISO timestamp`);
  }
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.valueOf()) || parsed.toISOString() !== value) {
    throw new EvidenceError(
      "INVALID_MANIFEST",
      `${at} must use canonical UTC ISO-8601 form`,
    );
  }
}

function assertMetadata(value: unknown): asserts value is JsonObject {
  if (!isObject(value)) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest.metadata must be an object");
  }
  try {
    canonicalJson(value);
  } catch (error) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest.metadata is not strict JSON", {
      cause: error,
    });
  }
}

function parseFile(value: unknown, index: number): EvidenceFileV1 {
  const at = `manifest.files[${index}]`;
  if (!isObject(value)) {
    throw new EvidenceError("INVALID_MANIFEST", `${at} must be an object`);
  }
  assertExactKeys(value, ["path", "size", "sha256", "mediaType"], ["role"], at);
  try {
    assertSafeEvidencePath(value.path);
  } catch (error) {
    if (error instanceof EvidenceError) throw error;
    throw new EvidenceError("INVALID_PATH", `${at}.path is unsafe`, { cause: error });
  }
  if (!Number.isSafeInteger(value.size) || (value.size as number) < 0) {
    throw new EvidenceError("INVALID_MANIFEST", `${at}.size must be a non-negative safe integer`);
  }
  if (!isSha256Digest(value.sha256)) {
    throw new EvidenceError("INVALID_MANIFEST", `${at}.sha256 is not a SHA-256 content ID`);
  }
  assertNonEmptyString(value.mediaType, `${at}.mediaType`, 255);
  if (Object.hasOwn(value, "role")) {
    assertNonEmptyString(value.role, `${at}.role`, 128);
  }

  const file: EvidenceFileV1 = {
    path: value.path,
    size: value.size as number,
    sha256: value.sha256,
    mediaType: value.mediaType,
  };
  if (typeof value.role === "string") file.role = value.role;
  return file;
}

export function manifestBodyOf(manifest: EvidenceManifestV1): EvidenceManifestBodyV1 {
  const body: EvidenceManifestBodyV1 = {
    schemaVersion: manifest.schemaVersion,
    runId: manifest.runId,
    createdAt: manifest.createdAt,
    files: manifest.files,
  };
  if (manifest.metadata !== undefined) body.metadata = manifest.metadata;
  return body;
}

export function assertEvidenceManifestV1(
  value: unknown,
): asserts value is EvidenceManifestV1 {
  if (!isObject(value)) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest must be an object");
  }
  assertExactKeys(
    value,
    ["schemaVersion", "manifestId", "runId", "createdAt", "files"],
    ["metadata"],
    "manifest",
  );
  if (value.schemaVersion !== EVIDENCE_MANIFEST_VERSION) {
    throw new EvidenceError(
      "INVALID_MANIFEST",
      `Unsupported evidence manifest version: ${String(value.schemaVersion)}`,
    );
  }
  if (!isSha256Digest(value.manifestId)) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest.manifestId is not a SHA-256 content ID");
  }
  assertNonEmptyString(value.runId, "manifest.runId", 256);
  assertCanonicalTimestamp(value.createdAt, "manifest.createdAt");
  if (!Array.isArray(value.files)) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest.files must be an array");
  }
  if (Object.hasOwn(value, "metadata")) assertMetadata(value.metadata);

  const files = value.files.map(parseFile);
  const seen = new Set<string>();
  let previousPath: string | undefined;
  for (const file of files) {
    const key = portablePathKey(file.path);
    if (seen.has(key)) {
      throw new EvidenceError("DUPLICATE_PATH", `Duplicate evidence path: ${file.path}`);
    }
    seen.add(key);
    if (previousPath !== undefined && previousPath > file.path) {
      throw new EvidenceError("INVALID_MANIFEST", "manifest.files must be sorted by path");
    }
    previousPath = file.path;
  }
}

export function parseEvidenceManifest(canonicalText: string): EvidenceManifestV1 {
  let parsed: unknown;
  try {
    parsed = JSON.parse(canonicalText) as unknown;
  } catch (error) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest.json is not valid JSON", {
      cause: error,
    });
  }
  assertEvidenceManifestV1(parsed);
  let reencoded: string;
  try {
    reencoded = canonicalJson(parsed);
  } catch (error) {
    throw new EvidenceError(
      "INVALID_MANIFEST",
      "manifest.json contains a value outside strict canonical JSON",
      { cause: error },
    );
  }
  if (reencoded !== canonicalText) {
    throw new EvidenceError(
      "INVALID_MANIFEST",
      "manifest.json is not encoded as canonical JSON",
    );
  }
  return parsed;
}

export function assertManifestId(manifest: EvidenceManifestV1): void {
  const expected = manifestIdFor(manifestBodyOf(manifest));
  if (manifest.manifestId !== expected) {
    throw new EvidenceError(
      "MANIFEST_ID_MISMATCH",
      `Manifest ID mismatch: expected ${expected}, got ${manifest.manifestId}`,
    );
  }
}
