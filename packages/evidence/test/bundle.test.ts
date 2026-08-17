import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import {
  canonicalJson,
  type EvidenceManifestBodyV1,
  type EvidenceManifestV1,
} from "@open-agent-lab/contracts";

import {
  EvidenceError,
  manifestBodyOf,
  manifestIdFor,
  verifyEvidenceBundle,
  writeEvidenceBundle,
} from "../src/index.js";

const temporaryDirectories: string[] = [];

async function temporaryDirectory(): Promise<string> {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-evidence-test-"));
  temporaryDirectories.push(directory);
  return directory;
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true }),
    ),
  );
});

async function readManifest(bundle: string): Promise<EvidenceManifestV1> {
  return JSON.parse(await readFile(join(bundle, "manifest.json"), "utf8")) as EvidenceManifestV1;
}

async function replaceManifest(
  bundle: string,
  mutate: (manifest: EvidenceManifestV1) => void,
  refreshId = true,
): Promise<void> {
  const manifest = await readManifest(bundle);
  mutate(manifest);
  if (refreshId) {
    manifest.manifestId = manifestIdFor(manifestBodyOf(manifest));
  }
  await writeFile(join(bundle, "manifest.json"), canonicalJson(manifest));
}

function rejectsWithCode(code: EvidenceError["code"]): (error: unknown) => boolean {
  return (error: unknown) => error instanceof EvidenceError && error.code === code;
}

test("writer publishes a deterministic bundle which strict verification accepts", async () => {
  const parent = await temporaryDirectory();
  const bundle = join(parent, "run.evidence");
  const createdAt = "2026-08-16T01:02:03.004Z";

  const manifest = await writeEvidenceBundle(bundle, {
    runId: "terminal-bench-task-1",
    createdAt,
    metadata: { model: "deepseek-chat", success: true },
    files: [
      {
        path: "logs/stdout.txt",
        content: "all tests passed\n",
        mediaType: "text/plain",
        role: "stdout",
      },
      {
        path: "events.jsonl",
        content: '{"sequence":0}\n',
        mediaType: "application/x-ndjson",
        role: "events",
      },
    ],
  });

  assert.equal(manifest.runId, "terminal-bench-task-1");
  assert.deepEqual(
    manifest.files.map((file) => file.path),
    ["events.jsonl", "logs/stdout.txt"],
  );
  assert.match(manifest.manifestId, /^sha256:[a-f0-9]{64}$/u);
  assert.equal(
    await readFile(join(bundle, "manifest.json"), "utf8"),
    canonicalJson(manifest),
  );

  const verified = await verifyEvidenceBundle(bundle);
  assert.equal(verified.manifest.manifestId, manifest.manifestId);
  assert.equal(verified.fileCount, 2);
  assert.equal(verified.totalBytes, 32);
});

test("writer rejects duplicate, traversal and non-finite inputs before publication", async () => {
  const parent = await temporaryDirectory();

  await assert.rejects(
    writeEvidenceBundle(join(parent, "duplicate"), {
      runId: "run",
      files: [
        { path: "Log.txt", content: "a" },
        { path: "log.txt", content: "b" },
      ],
    }),
    rejectsWithCode("DUPLICATE_PATH"),
  );
  await assert.rejects(
    writeEvidenceBundle(join(parent, "traversal"), {
      runId: "run",
      files: [{ path: "../outside", content: "no" }],
    }),
    rejectsWithCode("INVALID_PATH"),
  );
  await assert.rejects(
    writeEvidenceBundle(join(parent, "nonfinite"), {
      runId: "run",
      files: [],
      metadata: { score: Number.NaN },
    }),
    rejectsWithCode("INVALID_INPUT"),
  );
  assert.deepEqual(await readdir(parent), []);
});

test("writer cleans staging state when a filesystem conflict occurs", async () => {
  const parent = await temporaryDirectory();
  const target = join(parent, "atomic");

  await assert.rejects(
    writeEvidenceBundle(target, {
      runId: "run",
      files: [
        { path: "collision", content: "file" },
        { path: "collision/child", content: "cannot be below a file" },
      ],
    }),
    rejectsWithCode("IO_ERROR"),
  );
  assert.deepEqual(await readdir(parent), []);
});

test("writer never overwrites an existing target", async () => {
  const parent = await temporaryDirectory();
  const target = join(parent, "existing");
  await mkdir(target);
  await writeFile(join(target, "keep.txt"), "keep");

  await assert.rejects(
    writeEvidenceBundle(target, { runId: "run", files: [] }),
    rejectsWithCode("TARGET_EXISTS"),
  );
  assert.equal(await readFile(join(target, "keep.txt"), "utf8"), "keep");
});

test("verifier detects byte tampering by content hash", async () => {
  const parent = await temporaryDirectory();
  const bundle = join(parent, "tampered");
  await writeEvidenceBundle(bundle, {
    runId: "run",
    files: [{ path: "result.txt", content: "pass", mediaType: "text/plain" }],
  });
  await writeFile(join(bundle, "result.txt"), "fail");

  await assert.rejects(verifyEvidenceBundle(bundle), rejectsWithCode("HASH_MISMATCH"));
});

test("verifier detects declared-size and manifest-ID tampering", async () => {
  const parent = await temporaryDirectory();
  const sizeBundle = join(parent, "wrong-size");
  await writeEvidenceBundle(sizeBundle, {
    runId: "run",
    files: [{ path: "result.txt", content: "pass" }],
  });
  await replaceManifest(sizeBundle, (manifest) => {
    const first = manifest.files[0];
    assert.ok(first);
    first.size += 1;
  });
  await assert.rejects(verifyEvidenceBundle(sizeBundle), rejectsWithCode("SIZE_MISMATCH"));

  const idBundle = join(parent, "wrong-id");
  await writeEvidenceBundle(idBundle, { runId: "run", files: [] });
  await replaceManifest(
    idBundle,
    (manifest) => {
      manifest.runId = "silently-replaced";
    },
    false,
  );
  await assert.rejects(
    verifyEvidenceBundle(idBundle),
    rejectsWithCode("MANIFEST_ID_MISMATCH"),
  );
});

test("verifier rejects duplicate and traversal paths even with a valid manifest ID", async () => {
  const parent = await temporaryDirectory();
  const duplicateBundle = join(parent, "duplicate-manifest");
  await writeEvidenceBundle(duplicateBundle, {
    runId: "run",
    files: [{ path: "one.txt", content: "1" }],
  });
  await replaceManifest(duplicateBundle, (manifest) => {
    const first = manifest.files[0];
    assert.ok(first);
    manifest.files.push({ ...first });
  });
  await assert.rejects(
    verifyEvidenceBundle(duplicateBundle),
    rejectsWithCode("DUPLICATE_PATH"),
  );

  const traversalBundle = join(parent, "traversal-manifest");
  await writeEvidenceBundle(traversalBundle, {
    runId: "run",
    files: [{ path: "one.txt", content: "1" }],
  });
  const traversalManifest = await readManifest(traversalBundle);
  const first = traversalManifest.files[0];
  assert.ok(first);
  first.path = "../outside";
  const traversalBody = manifestBodyOf(traversalManifest) as EvidenceManifestBodyV1;
  traversalManifest.manifestId = manifestIdFor(traversalBody);
  await writeFile(
    join(traversalBundle, "manifest.json"),
    canonicalJson(traversalManifest),
  );
  await assert.rejects(
    verifyEvidenceBundle(traversalBundle),
    rejectsWithCode("INVALID_PATH"),
  );
});

test("verifier rejects undeclared files and symbolic links", async () => {
  const parent = await temporaryDirectory();
  const extraBundle = join(parent, "extra");
  await writeEvidenceBundle(extraBundle, { runId: "run", files: [] });
  await writeFile(join(extraBundle, "not-declared.txt"), "surprise");
  await assert.rejects(
    verifyEvidenceBundle(extraBundle),
    rejectsWithCode("UNDECLARED_ENTRY"),
  );

  const linkBundle = join(parent, "link");
  await writeEvidenceBundle(linkBundle, {
    runId: "run",
    files: [{ path: "artifact.txt", content: "inside" }],
  });
  await rm(join(linkBundle, "artifact.txt"));
  await symlink(join(parent, "outside.txt"), join(linkBundle, "artifact.txt"));
  await writeFile(join(parent, "outside.txt"), "inside");
  await assert.rejects(verifyEvidenceBundle(linkBundle), rejectsWithCode("UNSAFE_ENTRY"));
});

test("verifier enforces caller-provided resource limits", async () => {
  const parent = await temporaryDirectory();
  const bundle = join(parent, "limited");
  await writeEvidenceBundle(bundle, {
    runId: "run",
    files: [{ path: "four-bytes", content: "1234" }],
  });

  await assert.rejects(
    verifyEvidenceBundle(bundle, { maxFileBytes: 3 }),
    rejectsWithCode("LIMIT_EXCEEDED"),
  );
});
