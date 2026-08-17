import { EvidenceError } from "./errors.js";

export interface EvidenceLimits {
  maxFiles: number;
  maxFileBytes: number;
  maxTotalBytes: number;
  maxManifestBytes: number;
}

export type EvidenceLimitOptions = Partial<EvidenceLimits>;

export const DEFAULT_EVIDENCE_LIMITS: Readonly<EvidenceLimits> = Object.freeze({
  maxFiles: 10_000,
  maxFileBytes: 256 * 1024 * 1024,
  maxTotalBytes: 2 * 1024 * 1024 * 1024,
  maxManifestBytes: 8 * 1024 * 1024,
});

export function resolveEvidenceLimits(
  options: EvidenceLimitOptions | undefined,
): EvidenceLimits {
  const limits: EvidenceLimits = {
    ...DEFAULT_EVIDENCE_LIMITS,
    ...options,
  };

  for (const [name, value] of Object.entries(limits)) {
    if (!Number.isSafeInteger(value) || value <= 0) {
      throw new EvidenceError(
        "INVALID_INPUT",
        `${name} must be a positive safe integer`,
      );
    }
  }
  if (limits.maxFileBytes > limits.maxTotalBytes) {
    throw new EvidenceError(
      "INVALID_INPUT",
      "maxFileBytes cannot exceed maxTotalBytes",
    );
  }
  return limits;
}
