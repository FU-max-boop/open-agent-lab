import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EVIDENCE_MANIFEST_VERSION,
  type EvidenceManifestBodyV1,
} from "@open-agent-lab/contracts";

import { manifestIdFor, sha256 } from "../src/index.js";

test("sha256 returns a lowercase content-addressed digest", () => {
  assert.equal(
    sha256("abc"),
    "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  );
  assert.equal(sha256(new TextEncoder().encode("abc")), sha256("abc"));
});

test("manifest IDs are independent of object key insertion order", () => {
  const first: EvidenceManifestBodyV1 = {
    schemaVersion: EVIDENCE_MANIFEST_VERSION,
    runId: "run-1",
    createdAt: "2026-08-16T00:00:00.000Z",
    files: [],
    metadata: { z: 1, a: true },
  };
  const second = {
    metadata: { a: true, z: 1 },
    files: [],
    createdAt: "2026-08-16T00:00:00.000Z",
    runId: "run-1",
    schemaVersion: EVIDENCE_MANIFEST_VERSION,
  } satisfies EvidenceManifestBodyV1;

  assert.equal(manifestIdFor(first), manifestIdFor(second));
});
