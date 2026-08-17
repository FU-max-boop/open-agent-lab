import { createHash } from "node:crypto";

import type {
  EvidenceManifestBodyV1,
  Sha256Digest,
} from "@open-agent-lab/contracts";
import { canonicalJson } from "@open-agent-lab/contracts";

export function sha256(content: string | Uint8Array): Sha256Digest {
  return `sha256:${createHash("sha256").update(content).digest("hex")}`;
}

export function isSha256Digest(value: unknown): value is Sha256Digest {
  return typeof value === "string" && /^sha256:[a-f0-9]{64}$/u.test(value);
}

export function manifestIdFor(body: EvidenceManifestBodyV1): Sha256Digest {
  return sha256(canonicalJson(body));
}
