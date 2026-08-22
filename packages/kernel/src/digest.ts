import { createHash } from "node:crypto";

import { canonicalJson, type Sha256Digest } from "@open-agent-lab/contracts";

export const DIGEST_PATTERN = /^sha256:[a-f0-9]{64}$/u;

export function kernelDigest(value: unknown): Sha256Digest {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
