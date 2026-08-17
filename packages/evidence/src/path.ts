import { Buffer } from "node:buffer";

import { EvidenceError } from "./errors.js";

const MAX_PATH_BYTES = 4_096;
const MAX_SEGMENT_BYTES = 255;
const WINDOWS_DEVICE_NAME = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;

/**
 * Validates the portable path dialect used inside evidence bundles.
 * Backslashes, platform-specific absolute paths, dot segments, and names with
 * surprising cross-platform filesystem semantics are intentionally excluded.
 */
export function assertSafeEvidencePath(value: unknown): asserts value is string {
  if (typeof value !== "string" || value.length === 0) {
    throw new EvidenceError("INVALID_PATH", "Evidence path must be a non-empty string");
  }
  if (value !== value.normalize("NFC")) {
    throw new EvidenceError("INVALID_PATH", `Evidence path is not NFC: ${value}`);
  }
  if (Buffer.byteLength(value, "utf8") > MAX_PATH_BYTES) {
    throw new EvidenceError("INVALID_PATH", `Evidence path is too long: ${value}`);
  }
  if (value.startsWith("/") || value.includes("\\")) {
    throw new EvidenceError(
      "INVALID_PATH",
      `Evidence path must be a relative POSIX path: ${value}`,
    );
  }
  if (/[\u0000-\u001f\u007f]/u.test(value)) {
    throw new EvidenceError("INVALID_PATH", `Evidence path contains a control byte: ${value}`);
  }

  const segments = value.split("/");
  for (const segment of segments) {
    if (segment.length === 0 || segment === "." || segment === "..") {
      throw new EvidenceError("INVALID_PATH", `Evidence path has an unsafe segment: ${value}`);
    }
    if (
      segment.includes(":") ||
      segment.endsWith(".") ||
      segment.endsWith(" ") ||
      WINDOWS_DEVICE_NAME.test(segment)
    ) {
      throw new EvidenceError(
        "INVALID_PATH",
        `Evidence path is not portable across filesystems: ${value}`,
      );
    }
    if (Buffer.byteLength(segment, "utf8") > MAX_SEGMENT_BYTES) {
      throw new EvidenceError("INVALID_PATH", `Evidence path segment is too long: ${value}`);
    }
  }

  if (value.toLowerCase() === "manifest.json") {
    throw new EvidenceError("INVALID_PATH", "manifest.json is reserved by the bundle format");
  }
}

/** Case-folding prevents two entries from colliding on common filesystems. */
export function portablePathKey(value: string): string {
  return value.normalize("NFC").toLowerCase();
}
