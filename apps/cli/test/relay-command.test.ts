import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import {
  awaitRelayAuthorization,
  publishFileAtomic,
  readVerifiedBuildId,
  runRelayCommand,
} from "../src/relay-command.js";

async function waitForFile(path: string, attempts = 100): Promise<string> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await readFile(path, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  }
  throw new Error(`Timed out waiting for ${path}.`);
}

test("atomic publication never exposes partial file contents", async () => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-"));
  try {
    const path = join(directory, "published");
    const payload = `${"x".repeat(2 * 1024 * 1024)}\n`;
    const publishing = publishFileAtomic(path, payload);
    const observed = await waitForFile(path);
    await publishing;
    assert.equal(observed, payload);
    await assert.rejects(publishFileAtomic(path, "replacement\n"), /EEXIST/u);
    assert.equal(await readFile(path, "utf8"), payload);
  } finally {
    await rm(directory, { recursive: true });
  }
});

test("relay build identity is verified before secret-dependent startup", async () => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-"));
  try {
    const file = join(directory, "build-id");
    const buildId = `sha256:${"a".repeat(64)}`;
    const args = ["--build-id-file", file];
    await writeFile(file, `${buildId}\n`);

    assert.equal(
      await readVerifiedBuildId(args, { OAL_EXPECTED_RELAY_BUILD_ID: buildId }),
      buildId,
    );
    await assert.rejects(readVerifiedBuildId([], {}), /build-id-file is required/u);
    await assert.rejects(readVerifiedBuildId(args, {}), /expected preflight/u);
    await assert.rejects(
      readVerifiedBuildId(args, { OAL_EXPECTED_RELAY_BUILD_ID: `sha256:${"b".repeat(64)}` }),
      /expected preflight/u,
    );
  } finally {
    await rm(directory, { recursive: true });
  }
});

test("relay authorization gates all secret-dependent startup", async () => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-"));
  try {
    const buildFile = join(directory, "build-id");
    const sidecar = join(directory, "provider-metadata.ndjson");
    const buildId = `sha256:${"c".repeat(64)}`;
    const args = [
      "--provider",
      "deepseek",
      "--model",
      "deepseek-v4-pro",
      "--build-id-file",
      buildFile,
      "--output",
      sidecar,
    ];
    await writeFile(buildFile, `${buildId}\n`);

    const pending = awaitRelayAuthorization(
      args,
      { OAL_EXPECTED_RELAY_BUILD_ID: buildId },
      { provider: "deepseek", model: "deepseek-v4-pro" },
    );
    const bootstrap = JSON.parse(
      await waitForFile(`${sidecar}.bootstrap-ready`),
    ) as Record<string, unknown>;
    assert.deepEqual(Object.keys(bootstrap).sort(), [
      "buildId",
      "capabilityId",
      "model",
      "provider",
      "schemaVersion",
    ]);
    assert.equal(bootstrap.schemaVersion, 1);
    assert.equal(bootstrap.buildId, buildId);
    assert.equal(bootstrap.provider, "deepseek");
    assert.equal(bootstrap.model, "deepseek-v4-pro");
    assert.match(String(bootstrap.capabilityId), /^[a-f0-9]{64}$/u);
    process.emit("SIGUSR1", "SIGUSR1");
    assert.deepEqual(await pending, {
      buildId,
      readyPath: `${sidecar}.bootstrap-ready`,
      provider: "deepseek",
      model: "deepseek-v4-pro",
      capabilityId: bootstrap.capabilityId,
    });

    await assert.rejects(
      awaitRelayAuthorization(
        args,
        { OAL_EXPECTED_RELAY_BUILD_ID: buildId },
        { provider: "zai", model: "glm-5.3" },
      ),
      /expected provider and model/u,
    );
  } finally {
    await rm(directory, { recursive: true });
  }
});

test("relay authorization keeps an otherwise idle process alive", async () => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-"));
  const buildFile = join(directory, "build-id");
  const sidecar = join(directory, "provider-metadata.ndjson");
  const buildId = `sha256:${"c".repeat(64)}`;
  const source = pathToFileURL(join(process.cwd(), "apps/cli/src/relay-command.ts")).href;
  const script = `
    import { awaitRelayAuthorization } from ${JSON.stringify(source)};
    const args = ${JSON.stringify([
      "--provider",
      "deepseek",
      "--model",
      "deepseek-v4-pro",
      "--build-id-file",
      buildFile,
      "--output",
      sidecar,
    ])};
    await awaitRelayAuthorization(
      args,
      { OAL_EXPECTED_RELAY_BUILD_ID: ${JSON.stringify(buildId)} },
      { provider: "deepseek", model: "deepseek-v4-pro" },
    ).catch((error) => {
      if (!(error instanceof Error) || !error.message.includes("was interrupted")) throw error;
    });
  `;
  await writeFile(buildFile, `${buildId}\n`);
  const child = spawn(process.execPath, ["--import", "tsx", "--input-type=module", "--eval", script], {
    stdio: "ignore",
  });
  try {
    await waitForFile(`${sidecar}.bootstrap-ready`, 1_000);
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(child.exitCode, null);
    assert.equal(child.kill("SIGTERM"), true);
    const [code, signal] = await once(child, "exit");
    assert.equal(signal, null);
    assert.equal(code, 0);
  } finally {
    if (child.exitCode === null) {
      const exited = once(child, "exit");
      child.kill("SIGKILL");
      await exited;
    }
    await rm(directory, { recursive: true });
  }
});

test("token publication is non-overwriting and failed publication closes the relay", async () => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-"));
  try {
    const buildFile = join(directory, "build-id");
    const sidecar = join(directory, "provider-metadata.ndjson");
    const token = join(directory, "client-token");
    const readyPath = `${sidecar}.bootstrap-ready`;
    const buildId = `sha256:${"d".repeat(64)}`;
    const args = [
      "--provider",
      "test",
      "--model",
      "test-model",
      "--build-id-file",
      buildFile,
      "--output",
      sidecar,
      "--client-token-output",
      token,
      "--port",
      "0",
    ];
    await writeFile(buildFile, `${buildId}\n`);
    await writeFile(readyPath, "bootstrap\n");
    await writeFile(token, "existing-capability\n");

    await assert.rejects(
      runRelayCommand(
        args,
        { TEST_API_KEY: "provider-secret", OAL_EXPECTED_RELAY_BUILD_ID: buildId },
        () => assert.fail("relay must not announce a conflicting token"),
        {
          test: {
            envKey: "TEST_API_KEY",
            endpoint: "http://127.0.0.1:9/responses",
            models: ["test-model"],
          },
        },
        {
          buildId,
          readyPath,
          provider: "test",
          model: "test-model",
          capabilityId: "e".repeat(64),
        },
      ),
      /EEXIST/u,
    );
    assert.equal(await readFile(token, "utf8"), "existing-capability\n");
    assert.match(await readFile(`${sidecar}.sealed`, "utf8"), /"state":"sealed"/u);
  } finally {
    await rm(directory, { recursive: true });
  }
});
