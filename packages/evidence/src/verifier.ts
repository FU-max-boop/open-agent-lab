import { constants } from "node:fs";
import { lstat, open, readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { TextDecoder } from "node:util";

import type { EvidenceManifestV1 } from "@open-agent-lab/contracts";

import { sha256 } from "./digest.js";
import { EvidenceError } from "./errors.js";
import {
  resolveEvidenceLimits,
  type EvidenceLimitOptions,
} from "./limits.js";
import {
  assertManifestId,
  parseEvidenceManifest,
} from "./manifest.js";

export type VerifyEvidenceBundleOptions = EvidenceLimitOptions;

export interface VerifiedEvidenceBundle {
  manifest: EvidenceManifestV1;
  fileCount: number;
  totalBytes: number;
}

async function readRegularFileNoFollow(
  path: string,
  maxBytes: number,
  displayPath: string,
): Promise<Uint8Array> {
  const noFollow = constants.O_NOFOLLOW ?? 0;
  let handle;
  try {
    handle = await open(path, constants.O_RDONLY | noFollow);
  } catch (error) {
    throw new EvidenceError("UNSAFE_ENTRY", `Cannot safely open ${displayPath}`, {
      cause: error,
    });
  }
  try {
    const info = await handle.stat();
    if (!info.isFile()) {
      throw new EvidenceError("UNSAFE_ENTRY", `${displayPath} is not a regular file`);
    }
    if (info.size > maxBytes) {
      throw new EvidenceError(
        "LIMIT_EXCEEDED",
        `${displayPath} exceeds the ${maxBytes}-byte read limit`,
      );
    }
    const content = await handle.readFile();
    if (content.byteLength !== info.size) {
      throw new EvidenceError(
        "SIZE_MISMATCH",
        `${displayPath} changed while it was being read`,
      );
    }
    return content;
  } finally {
    await handle.close();
  }
}

function expectedDirectories(paths: readonly string[]): Set<string> {
  const directories = new Set<string>();
  for (const path of paths) {
    const segments = path.split("/");
    for (let length = 1; length < segments.length; length += 1) {
      directories.add(segments.slice(0, length).join("/"));
    }
  }
  return directories;
}

async function verifyInventory(
  root: string,
  expectedFiles: Set<string>,
  expectedDirs: Set<string>,
): Promise<Set<string>> {
  const seen = new Set<string>();

  async function visit(directory: string, relativeDirectory: string): Promise<void> {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (const entry of entries) {
      const relative = relativeDirectory
        ? `${relativeDirectory}/${entry.name}`
        : entry.name;
      const fullPath = join(directory, entry.name);
      const info = await lstat(fullPath);
      if (info.isSymbolicLink()) {
        throw new EvidenceError("UNSAFE_ENTRY", `Symbolic links are forbidden: ${relative}`);
      }
      if (info.isDirectory()) {
        if (!expectedDirs.has(relative)) {
          throw new EvidenceError("UNDECLARED_ENTRY", `Undeclared directory: ${relative}`);
        }
        await visit(fullPath, relative);
      } else if (info.isFile()) {
        if (relative !== "manifest.json" && !expectedFiles.has(relative)) {
          throw new EvidenceError("UNDECLARED_ENTRY", `Undeclared file: ${relative}`);
        }
        seen.add(relative);
      } else {
        throw new EvidenceError("UNSAFE_ENTRY", `Special filesystem entry: ${relative}`);
      }
    }
  }

  await visit(root, "");
  return seen;
}

/** Fully validates structure, limits, canonical encoding, IDs and every byte. */
export async function verifyEvidenceBundle(
  bundleDirectory: string,
  options?: VerifyEvidenceBundleOptions,
): Promise<VerifiedEvidenceBundle> {
  if (typeof bundleDirectory !== "string" || bundleDirectory.length === 0) {
    throw new EvidenceError("INVALID_INPUT", "bundleDirectory must be non-empty");
  }
  const limits = resolveEvidenceLimits(options);
  const root = resolve(bundleDirectory);
  let rootInfo;
  try {
    rootInfo = await lstat(root);
  } catch (error) {
    throw new EvidenceError("IO_ERROR", `Cannot inspect evidence bundle ${root}`, {
      cause: error,
    });
  }
  if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) {
    throw new EvidenceError("UNSAFE_ENTRY", "Evidence bundle root must be a real directory");
  }

  const manifestContent = await readRegularFileNoFollow(
    join(root, "manifest.json"),
    limits.maxManifestBytes,
    "manifest.json",
  );
  let manifestText: string;
  try {
    manifestText = new TextDecoder("utf-8", { fatal: true }).decode(manifestContent);
  } catch (error) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest.json is not valid UTF-8", {
      cause: error,
    });
  }
  const manifest = parseEvidenceManifest(manifestText);
  assertManifestId(manifest);

  if (manifest.files.length > limits.maxFiles) {
    throw new EvidenceError(
      "LIMIT_EXCEEDED",
      `Manifest has ${manifest.files.length} files; maximum is ${limits.maxFiles}`,
    );
  }

  let totalBytes = 0;
  for (const file of manifest.files) {
    if (file.size > limits.maxFileBytes) {
      throw new EvidenceError(
        "LIMIT_EXCEEDED",
        `${file.path} exceeds maxFileBytes (${limits.maxFileBytes})`,
      );
    }
    totalBytes += file.size;
    if (!Number.isSafeInteger(totalBytes) || totalBytes > limits.maxTotalBytes) {
      throw new EvidenceError(
        "LIMIT_EXCEEDED",
        `Bundle exceeds maxTotalBytes (${limits.maxTotalBytes})`,
      );
    }
  }

  const expectedFiles = new Set(manifest.files.map((file) => file.path));
  const expectedDirs = expectedDirectories([...expectedFiles]);
  const seen = await verifyInventory(root, expectedFiles, expectedDirs);
  if (!seen.has("manifest.json")) {
    throw new EvidenceError("INVALID_MANIFEST", "manifest.json is missing");
  }

  for (const file of manifest.files) {
    if (!seen.has(file.path)) {
      throw new EvidenceError("SIZE_MISMATCH", `Declared evidence file is missing: ${file.path}`);
    }
    const content = await readRegularFileNoFollow(
      join(root, ...file.path.split("/")),
      limits.maxFileBytes,
      file.path,
    );
    if (content.byteLength !== file.size) {
      throw new EvidenceError(
        "SIZE_MISMATCH",
        `Size mismatch for ${file.path}: expected ${file.size}, got ${content.byteLength}`,
      );
    }
    const actualDigest = sha256(content);
    if (actualDigest !== file.sha256) {
      throw new EvidenceError(
        "HASH_MISMATCH",
        `Hash mismatch for ${file.path}: expected ${file.sha256}, got ${actualDigest}`,
      );
    }
  }

  return {
    manifest,
    fileCount: manifest.files.length,
    totalBytes,
  };
}
