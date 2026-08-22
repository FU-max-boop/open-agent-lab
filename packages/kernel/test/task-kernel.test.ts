import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type { JsonObject, Sha256Digest } from "@open-agent-lab/contracts";
import { ToolBroker, ToolBrokerError, type ToolDefinition } from "@open-agent-lab/tool-broker";
import Database from "better-sqlite3";

import { KernelError, TaskKernel, type ReviewDecision } from "../src/index.js";

const sha = (character: string): Sha256Digest =>
  `sha256:${character.repeat(64)}` as Sha256Digest;
const TASK = sha("1");
const STATE_A = sha("a");
const STATE_B = sha("b");
const CONTRACT_A = sha("c");
const CONTRACT_B = sha("d");
const WORKSPACE = sha("f");

async function runDirectory(t: test.TestContext): Promise<string> {
  const parent = await mkdtemp(join(tmpdir(), "open-agent-lab-kernel-"));
  t.after(async () => rm(parent, { recursive: true, force: true }));
  return join(parent, "run");
}

function tool(overrides: Partial<ToolDefinition> = {}): ToolDefinition {
  return {
    name: "workspace.read",
    contractDigest: CONTRACT_A,
    effect: "read_only",
    stateFingerprint: async () => STATE_A,
    execute: async () => ({ output: { ok: true } }),
    ...overrides,
  };
}

test("idempotent intent survives interruption with one stable runtime key", async (t) => {
  const directory = await runDirectory(t);
  const keys: string[] = [];
  const remote = new Set<string>();
  let executions = 0;
  let interrupt = true;
  const broker = new ToolBroker([tool({
    name: "remote.put",
    effect: "idempotent",
    execute: async (invocation) => {
      executions += 1;
      assert.ok(invocation.idempotencyKey);
      keys.push(invocation.idempotencyKey);
      remote.add(invocation.idempotencyKey);
      if (interrupt) {
        interrupt = false;
        throw new Error("interrupted after effect");
      }
      return { output: { stored: true } };
    },
  })]);
  const first = await TaskKernel.create({ directory, runId: "idempotent", taskDigest: TASK, broker });
  await assert.rejects(first.invoke({ invocationId: "call-1", toolName: "remote.put", arguments: { n: 1 } }));
  await first.close();

  const resumed = await TaskKernel.open({ directory, runId: "idempotent", broker });
  assert.equal((await resumed.resume()).action, "replayed");
  assert.equal(executions, 2);
  assert.equal(remote.size, 1);
  assert.equal(new Set(keys).size, 1);
  assert.equal(resumed.state.completed.length, 1);
  await resumed.close();

  const otherDirectory = await runDirectory(t);
  const other = await TaskKernel.create({
    directory: otherDirectory,
    runId: "idempotent-other-run",
    taskDigest: TASK,
    broker,
  });
  await other.invoke({ invocationId: "call-1", toolName: "remote.put", arguments: { n: 1 } });
  assert.equal(new Set(keys).size, 2, "idempotency keys must be scoped to one run");
  assert.equal(remote.size, 2);
  await other.close();
});

test("uncertain external effects require one explicit journaled decision", async (t) => {
  for (const action of ["confirmed_applied", "confirmed_not_applied_then_retry", "abort"] as const) {
    await t.test(action, async (st) => {
      const directory = await runDirectory(st);
      let executions = 0;
      let interrupt = true;
      const broker = new ToolBroker([tool({
        name: "external.send",
        effect: "external_non_idempotent",
        execute: async () => {
          executions += 1;
          if (interrupt) {
            interrupt = false;
            throw new Error("interrupted after send");
          }
          return { output: { sent: true } };
        },
      })]);
      const first = await TaskKernel.create({ directory, runId: action, taskDigest: TASK, broker });
      await assert.rejects(first.invoke({ invocationId: "send-1", toolName: "external.send", arguments: {} }));
      await first.close();
      const kernel = await TaskKernel.open({ directory, runId: action, broker });
      assert.equal((await kernel.resume()).action, "needs_review");
      assert.equal(executions, 1);
      if (action === "confirmed_applied") {
        await kernel.resolveReview({
          action,
          operator: "tester",
          reason: "receipt observed",
          result: { output: { sent: true } },
        });
        assert.equal(executions, 1);
        assert.equal(kernel.state.completed.length, 1);
      } else if (action === "confirmed_not_applied_then_retry") {
        await kernel.resolveReview({ action, operator: "tester", reason: "no receipt" });
        assert.equal(executions, 2);
        assert.equal(kernel.state.completed.length, 1);
      } else {
        await kernel.resolveReview({ action, operator: "tester", reason: "do not retry" });
        assert.equal(kernel.state.lifecycle, "failed");
      }
      await kernel.close();
      const reopened = await TaskKernel.open({ directory, runId: action, broker });
      assert.equal(reopened.state.pending, undefined);
      if (action === "abort") {
        assert.equal(reopened.state.lifecycle, "failed");
      } else {
        assert.equal(reopened.state.lifecycle, "running");
        assert.equal(reopened.state.completed.length, 1);
        const beforeResume = executions;
        assert.equal((await reopened.resume()).action, "nothing_pending");
        assert.equal(executions, beforeResume);
      }
      await reopened.close();
    });
  }
});

test("review results preserve the JSON-object metadata contract", async (t) => {
  const directory = await runDirectory(t);
  const broker = new ToolBroker([tool({
    name: "external.send",
    effect: "external_non_idempotent",
    execute: async () => { throw new Error("uncertain send"); },
  })]);
  const first = await TaskKernel.create({
    directory,
    runId: "review-metadata",
    taskDigest: TASK,
    broker,
  });
  await assert.rejects(first.invoke({
    invocationId: "send-1",
    toolName: "external.send",
    arguments: {},
  }));
  await first.close();
  const kernel = await TaskKernel.open({ directory, runId: "review-metadata", broker });
  assert.equal((await kernel.resume()).action, "needs_review");
  for (const decision of [
    { action: "typo", operator: "tester", reason: "mistyped" },
    { operator: "tester", reason: "missing action" },
    null,
  ]) {
    await assert.rejects(
      kernel.resolveReview(decision as unknown as ReviewDecision),
      (error: unknown) => error instanceof KernelError && error.code === "invalid_state",
    );
    assert.equal(kernel.state.lifecycle, "needs_review");
    assert.equal(kernel.state.completed.length, 0);
  }
  for (const metadata of [null, []]) {
    await assert.rejects(kernel.resolveReview({
      action: "confirmed_applied",
      operator: "tester",
      reason: "receipt observed",
      result: { output: null, metadata: metadata as unknown as JsonObject },
    }));
    assert.equal(kernel.state.lifecycle, "needs_review");
    assert.equal(kernel.state.completed.length, 0);
  }
  await kernel.resolveReview({
    action: "confirmed_applied",
    operator: "tester",
    reason: "receipt observed",
    result: { output: null, metadata: { receipt: "confirmed" } },
  });
  await kernel.close();
});

test("workspace mutation recovery follows reconciliation", async (t) => {
  for (const status of ["applied", "not_applied", "unknown"] as const) {
    await t.test(status, async (st) => {
      const directory = await runDirectory(st);
      let executions = 0;
      let firstAttempt = true;
      const broker = new ToolBroker([tool({
        name: "workspace.write",
        effect: "workspace_mutation",
        execute: async () => {
          executions += 1;
          if (firstAttempt) {
            firstAttempt = false;
            throw new Error("interrupted at effect boundary");
          }
          return { output: { written: true } };
        },
        reconcile: async () => {
          if (status === "applied") return { status, result: { output: { written: true } } };
          if (status === "not_applied") return { status };
          return { status, reason: "workspace postcondition is ambiguous" };
        },
      })]);
      const first = await TaskKernel.create({ directory, runId: status, taskDigest: TASK, broker });
      await assert.rejects(first.invoke({ invocationId: "write-1", toolName: "workspace.write", arguments: {} }));
      await first.close();
      const kernel = await TaskKernel.open({ directory, runId: status, broker });
      const outcome = await kernel.resume();
      assert.equal(outcome.action, status === "applied" ? "reconciled" : status === "not_applied" ? "replayed" : "needs_review");
      assert.equal(executions, status === "not_applied" ? 2 : 1);
      await kernel.close();
    });
  }
});

test("writer lease rejects concurrency and fences an expired owner", async (t) => {
  const directory = await runDirectory(t);
  let now = 1_000;
  const broker = new ToolBroker([]);
  const first = await TaskKernel.create({
    directory,
    runId: "lease",
    taskDigest: TASK,
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  await assert.rejects(
    TaskKernel.open({ directory, runId: "lease", broker, leaseMs: 1_000, now: () => now }),
    (error: unknown) => error instanceof KernelError && error.code === "lease_held",
  );
  now = 2_001;
  const successor = await TaskKernel.open({
    directory,
    runId: "lease",
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  await assert.rejects(first.cancel("stale writer"),
    (error: unknown) => error instanceof KernelError && error.code === "lease_lost");
  assert.equal((await successor.cancel("current writer")).lifecycle, "cancelled");
  await first.close();
  await successor.close();

  const idleDirectory = await runDirectory(t);
  now = 10_000;
  const idle = await TaskKernel.create({
    directory: idleDirectory,
    runId: "idle-lease",
    taskDigest: TASK,
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  now = 11_001;
  assert.equal((await idle.cancel("still the only writer")).lifecycle, "cancelled");
  await idle.close();
});

test("expired takeover never races a paused workspace mutation", async (t) => {
  const directory = await runDirectory(t);
  let now = 1_000;
  let started!: () => void;
  let release!: () => void;
  const atBoundary = new Promise<void>((resolve) => { started = resolve; });
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  let effects = 0;
  let reconciliations = 0;
  const broker = new ToolBroker([tool({
    name: "workspace.write",
    effect: "workspace_mutation",
    execute: async () => {
      started();
      await blocked;
      effects += 1;
      return { output: { written: true } };
    },
    reconcile: async () => {
      reconciliations += 1;
      return { status: "not_applied" };
    },
  })]);
  const first = await TaskKernel.create({
    directory,
    runId: "paused-writer",
    taskDigest: TASK,
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  const oldInvocation = first.invoke({
    invocationId: "write-1",
    toolName: "workspace.write",
    arguments: {},
  });
  await atBoundary;
  now = 2_001;
  const successor = await TaskKernel.open({
    directory,
    runId: "paused-writer",
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  assert.equal(successor.state.lifecycle, "needs_review");
  await successor.close();
  const third = await TaskKernel.open({
    directory,
    runId: "paused-writer",
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  assert.equal((await third.resume()).action, "needs_review");
  assert.equal(reconciliations, 0);
  release();
  await assert.rejects(oldInvocation,
    (error: unknown) => error instanceof KernelError && error.code === "lease_lost");
  assert.equal(effects, 1);
  await first.close();
  await third.close();
});

test("failed takeover review leaves an expired marker for the next writer", async (t) => {
  const directory = await runDirectory(t);
  let now = 1_000;
  let started!: () => void;
  let release!: () => void;
  const atBoundary = new Promise<void>((resolve) => { started = resolve; });
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  let effects = 0;
  let reconciliations = 0;
  const broker = new ToolBroker([tool({
    name: "workspace.write",
    effect: "workspace_mutation",
    execute: async () => {
      started();
      await blocked;
      effects += 1;
      return { output: { written: true } };
    },
    reconcile: async () => {
      reconciliations += 1;
      return { status: "not_applied" };
    },
  })]);
  const old = await TaskKernel.create({
    directory,
    runId: "failed-takeover",
    taskDigest: TASK,
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  const oldInvocation = old.invoke({
    invocationId: "write-1",
    toolName: "workspace.write",
    arguments: {},
  });
  await atBoundary;
  now = 2_001;
  await assert.rejects(TaskKernel.open({
    directory,
    runId: "failed-takeover",
    broker,
    leaseMs: 1_000,
    now: () => now,
    clock: () => { throw new Error("clock unavailable"); },
  }), /clock unavailable/u);
  const successor = await TaskKernel.open({
    directory,
    runId: "failed-takeover",
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  assert.equal((await successor.resume()).action, "needs_review");
  assert.equal(reconciliations, 0);
  release();
  await assert.rejects(
    oldInvocation,
    (error: unknown) => error instanceof KernelError && error.code === "lease_lost",
  );
  assert.equal(effects, 1);
  await old.close();
  await successor.close();
});

test("expired takeover does not taint effects created by the successor", async (t) => {
  const directory = await runDirectory(t);
  let now = 1_000;
  let executions = 0;
  let reconciliations = 0;
  const broker = new ToolBroker([tool({
    name: "workspace.write",
    effect: "workspace_mutation",
    execute: async () => {
      executions += 1;
      if (executions === 1) throw new Error("successor interrupted after intent");
      return { output: { written: true } };
    },
    reconcile: async () => {
      reconciliations += 1;
      return { status: "not_applied" };
    },
  })]);
  const old = await TaskKernel.create({
    directory,
    runId: "idle-takeover",
    taskDigest: TASK,
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  now = 2_001;
  const successor = await TaskKernel.open({
    directory,
    runId: "idle-takeover",
    broker,
    leaseMs: 1_000,
    now: () => now,
  });
  await assert.rejects(successor.invoke({
    invocationId: "successor-write",
    toolName: "workspace.write",
    arguments: {},
  }));
  assert.equal((await successor.resume()).action, "replayed");
  assert.equal(reconciliations, 1);
  assert.equal(executions, 2);
  await old.close();
  await successor.close();
});

test("checkpoint is disposable while journal corruption fails closed", async (t) => {
  const directory = await runDirectory(t);
  const broker = new ToolBroker([tool()]);
  const first = await TaskKernel.create({ directory, runId: "corruption", taskDigest: TASK, broker });
  await first.invoke({ invocationId: "read-1", toolName: "workspace.read", arguments: {} });
  await first.close();

  const database = new Database(join(directory, "run.sqlite3"));
  database.prepare("UPDATE checkpoint SET checkpoint_json = '{}' WHERE singleton = 1").run();
  database.close();
  const recovered = await TaskKernel.open({ directory, runId: "corruption", broker });
  assert.equal(recovered.state.completed.length, 1);
  await recovered.close();

  const missing = new Database(join(directory, "run.sqlite3"));
  missing.exec("DROP TABLE checkpoint");
  missing.close();
  const rebuiltMissing = await TaskKernel.open({ directory, runId: "corruption", broker });
  assert.equal(rebuiltMissing.state.completed.length, 1);
  await rebuiltMissing.close();

  const view = new Database(join(directory, "run.sqlite3"));
  view.exec(`DROP TABLE checkpoint;
    CREATE VIEW checkpoint AS SELECT
      1 AS singleton,
      0 AS sequence,
      '' AS event_hash,
      '' AS checkpoint_hash,
      '' AS checkpoint_json`);
  view.close();
  const rebuilt = await TaskKernel.open({ directory, runId: "corruption", broker });
  assert.equal(rebuilt.state.completed.length, 1);

  const guarded = new Database(join(directory, "run.sqlite3"));
  guarded.exec(`CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON checkpoint
    BEGIN SELECT RAISE(FAIL, 'checkpoint rejected'); END`);
  const before = (guarded.prepare("SELECT count(*) AS count FROM events").get() as { count: number }).count;
  guarded.close();
  await assert.rejects(rebuilt.invoke({
    invocationId: "read-2",
    toolName: "workspace.read",
    arguments: { path: "other" },
  }));
  const inspected = new Database(join(directory, "run.sqlite3"));
  const after = (inspected.prepare("SELECT count(*) AS count FROM events").get() as { count: number }).count;
  inspected.exec("DROP TRIGGER reject_checkpoint");
  inspected.close();
  assert.equal(after, before, "event insert must roll back with checkpoint failure");
  await rebuilt.close();

  const damaged = new Database(join(directory, "run.sqlite3"));
  damaged.prepare("UPDATE events SET event_json = '{}' WHERE sequence = 0").run();
  damaged.close();
  await assert.rejects(
    TaskKernel.open({ directory, runId: "corruption", broker }),
    (error: unknown) => error instanceof KernelError && error.code === "corrupt_journal",
  );
});

test("invalid create options leave no target and incomplete initialization is repairable", async (t) => {
  const invalidDirectory = await runDirectory(t);
  const broker = new ToolBroker([]);
  await assert.rejects(TaskKernel.create({
    directory: invalidDirectory,
    runId: "invalid-create",
    taskDigest: TASK,
    broker,
    leaseMs: 0,
  }));
  const valid = await TaskKernel.create({
    directory: invalidDirectory,
    runId: "invalid-create",
    taskDigest: TASK,
    broker,
  });
  await valid.close();

  const overflowDirectory = await runDirectory(t);
  await assert.rejects(TaskKernel.create({
    directory: overflowDirectory,
    runId: "overflow-create",
    taskDigest: TASK,
    broker,
    leaseMs: 1,
    now: () => Number.MAX_SAFE_INTEGER,
  }));
  const afterOverflow = await TaskKernel.create({
    directory: overflowDirectory,
    runId: "overflow-create",
    taskDigest: TASK,
    broker,
  });
  await afterOverflow.close();

  const repairDirectory = await runDirectory(t);
  await assert.rejects(TaskKernel.create({
    directory: repairDirectory,
    runId: "repair-create",
    taskDigest: TASK,
    broker,
    createdAt: "2026-01-01T00:00:00.000Z",
    clock: () => { throw new Error("interrupted before run.created"); },
  }));
  const repaired = await TaskKernel.open({
    directory: repairDirectory,
    runId: "repair-create",
    broker,
  });
  assert.equal(repaired.state.lifecycle, "running");
  await repaired.close();

  const occupiedDirectory = await runDirectory(t);
  await mkdir(occupiedDirectory);
  await writeFile(join(occupiedDirectory, "owned.txt"), "keep me\n");
  await assert.rejects(TaskKernel.create({
    directory: occupiedDirectory,
    runId: "no-clobber",
    taskDigest: TASK,
    broker,
  }), (error: unknown) => error instanceof KernelError && error.code === "target_exists");
  assert.equal(await readFile(join(occupiedDirectory, "owned.txt"), "utf8"), "keep me\n");
});

test("action identity is conflict-safe, state-sensitive, and contract-bound", async (t) => {
  const directory = await runDirectory(t);
  let currentState = STATE_A;
  let executions = 0;
  const broker = new ToolBroker([tool({
    stateFingerprint: async () => currentState,
    execute: async () => { executions += 1; return { output: { executions } }; },
  })]);
  const kernel = await TaskKernel.create({ directory, runId: "identity", taskDigest: TASK, broker });
  const request = { toolName: "workspace.read", arguments: { path: "a" } } as const;
  await kernel.invoke({ invocationId: "call-1", ...request });
  await kernel.invoke({ invocationId: "call-2", ...request });
  assert.equal(executions, 1);
  await assert.rejects(
    kernel.invoke({ invocationId: "call-1", toolName: "workspace.read", arguments: { path: "b" } }),
    (error: unknown) => error instanceof KernelError && error.code === "invocation_conflict",
  );
  await assert.rejects(
    kernel.invoke({ invocationId: "call-2", toolName: "workspace.read", arguments: { path: "b" } }),
    (error: unknown) => error instanceof KernelError && error.code === "invocation_conflict",
  );
  currentState = STATE_B;
  await kernel.invoke({ invocationId: "call-3", ...request });
  assert.equal(executions, 2);
  await kernel.close();

  const driftDirectory = await runDirectory(t);
  const v1 = new ToolBroker([tool({ execute: async () => { throw new Error("interrupted"); } })]);
  const pending = await TaskKernel.create({ directory: driftDirectory, runId: "drift", taskDigest: TASK, broker: v1 });
  await assert.rejects(pending.invoke({ invocationId: "call-1", ...request }));
  await pending.close();
  let v2Executions = 0;
  const v2 = new ToolBroker([tool({ contractDigest: CONTRACT_B, execute: async () => {
    v2Executions += 1;
    return { output: null };
  } })]);
  const changed = await TaskKernel.open({ directory: driftDirectory, runId: "drift", broker: v2 });
  assert.equal((await changed.resume()).action, "needs_review");
  assert.equal(v2Executions, 0);
  await changed.close();

  const externalDirectory = await runDirectory(t);
  let sends = 0;
  const external = new ToolBroker([tool({
    name: "external.send",
    effect: "external_non_idempotent",
    execute: async () => { sends += 1; return { output: { sent: true } }; },
  })]);
  const externalKernel = await TaskKernel.create({
    directory: externalDirectory,
    runId: "intentional-repeat",
    taskDigest: TASK,
    broker: external,
  });
  await externalKernel.invoke({ invocationId: "send-1", toolName: "external.send", arguments: {} });
  await externalKernel.invoke({ invocationId: "send-2", toolName: "external.send", arguments: {} });
  assert.equal(sends, 2, "distinct external invocations must not be deduplicated");
  await externalKernel.close();
});

test("broker rejects state drift observed before execution", async (t) => {
  const directory = await runDirectory(t);
  let observations = 0;
  let executions = 0;
  const broker = new ToolBroker([tool({
    stateFingerprint: async () => {
      observations += 1;
      return observations === 1 ? STATE_A : STATE_B;
    },
    execute: async () => { executions += 1; return { output: null }; },
  })]);
  const kernel = await TaskKernel.create({
    directory,
    runId: "precondition",
    taskDigest: TASK,
    broker,
  });
  await assert.rejects(
    kernel.invoke({ invocationId: "read-1", toolName: "workspace.read", arguments: {} }),
    (error: unknown) => error instanceof ToolBrokerError && error.code === "precondition_changed",
  );
  assert.equal(executions, 0);
  assert.equal((await kernel.resume()).action, "needs_review");
  assert.equal((await kernel.cancel("discard failed read")).lifecycle, "cancelled");
  assert.equal(kernel.state.pending, undefined);
  await kernel.close();
});

test("verifier crashes are retryable and terminal success is bound", async (t) => {
  const directory = await runDirectory(t);
  const broker = new ToolBroker([]);
  const first = await TaskKernel.create({ directory, runId: "verify", taskDigest: TASK, broker });
  await assert.rejects(first.verify({
    id: "tests",
    version: "1",
    verify: async () => { throw new Error("verifier crashed"); },
  }));
  assert.equal(first.state.lifecycle, "running");
  await assert.rejects(first.verify({
    id: "tests",
    version: "1",
    verify: async () => ({ workspaceDigest: "sha256:bad" as Sha256Digest, passed: true }),
  }), (error: unknown) => error instanceof KernelError && error.code === "verifier_mismatch");
  await first.close();

  const reopened = await TaskKernel.open({ directory, runId: "verify", broker });
  assert.equal(reopened.state.lifecycle, "running");
  const record = await reopened.verify({
    id: "tests",
    version: "1",
    verify: async () => ({ workspaceDigest: WORKSPACE, passed: true, details: { exact: true } }),
  });
  assert.deepEqual(
    [record.runId, record.taskDigest, record.workspaceDigest, record.verifierId, record.verifierVersion],
    ["verify", TASK, WORKSPACE, "tests", "1"],
  );
  assert.equal(reopened.state.lifecycle, "succeeded");
  await reopened.close();
});

test("cancellation aborts cooperative work without discarding effect uncertainty", async (t) => {
  const directory = await runDirectory(t);
  let started!: () => void;
  const atEffect = new Promise<void>((resolve) => { started = resolve; });
  const broker = new ToolBroker([tool({
    effect: "workspace_mutation",
    execute: async (_invocation, context) => {
    started();
    return new Promise((_resolve, reject) => {
      context.signal?.addEventListener("abort", () => reject(new Error("cooperatively aborted")), {
        once: true,
      });
    });
  } })]);
  const kernel = await TaskKernel.create({ directory, runId: "cancel", taskDigest: TASK, broker });
  const invocation = kernel.invoke({ invocationId: "call-1", toolName: "workspace.read", arguments: {} });
  await atEffect;
  const cancellation = kernel.cancel("operator requested stop");
  await assert.rejects(invocation, /cooperatively aborted/u);
  const finalState = await cancellation;
  assert.equal(finalState.lifecycle, "needs_review");
  assert.equal(finalState.completed.length, 0);
  assert.ok(finalState.pending);
  await kernel.resolveReview({
    action: "abort",
    operator: "tester",
    reason: "cancelled with unresolved read boundary",
  });
  await kernel.close();
});

test("cancellation discards an interrupted read-only intent", async (t) => {
  const directory = await runDirectory(t);
  let started!: () => void;
  const atRead = new Promise<void>((resolve) => { started = resolve; });
  const broker = new ToolBroker([tool({ execute: async (_invocation, context) => {
    started();
    return new Promise((_resolve, reject) => {
      context.signal?.addEventListener("abort", () => reject(new Error("read aborted")), {
        once: true,
      });
    });
  } })]);
  const kernel = await TaskKernel.create({
    directory,
    runId: "cancel-read",
    taskDigest: TASK,
    broker,
  });
  const invocation = kernel.invoke({
    invocationId: "read-1",
    toolName: "workspace.read",
    arguments: {},
  });
  await atRead;
  const cancellation = kernel.cancel("read no longer needed");
  await assert.rejects(invocation, /read aborted/u);
  const state = await cancellation;
  assert.equal(state.lifecycle, "cancelled");
  assert.equal(state.pending, undefined);
  await kernel.close();
});

test("immediate cancellation stops queued invoke and verify work", async (t) => {
  const invokeDirectory = await runDirectory(t);
  let executions = 0;
  const broker = new ToolBroker([tool({
    execute: async () => {
      executions += 1;
      return { output: null };
    },
  })]);
  const invokeKernel = await TaskKernel.create({
    directory: invokeDirectory,
    runId: "cancel-queued-invoke",
    taskDigest: TASK,
    broker,
  });
  const invocation = invokeKernel.invoke({
    invocationId: "call-1",
    toolName: "workspace.read",
    arguments: {},
  });
  const invokeCancellation = invokeKernel.cancel("cancel queued invoke");
  await assert.rejects(invocation, /Cancellation was requested/u);
  assert.equal((await invokeCancellation).lifecycle, "cancelled");
  assert.equal(executions, 0);
  await invokeKernel.close();

  const verifyDirectory = await runDirectory(t);
  let verifications = 0;
  const verifyKernel = await TaskKernel.create({
    directory: verifyDirectory,
    runId: "cancel-queued-verify",
    taskDigest: TASK,
    broker: new ToolBroker([]),
  });
  const verification = verifyKernel.verify({
    id: "tests",
    version: "1",
    verify: async () => {
      verifications += 1;
      return { workspaceDigest: WORKSPACE, passed: true };
    },
  });
  const verifyCancellation = verifyKernel.cancel("cancel queued verifier");
  await assert.rejects(verification, /Cancellation was requested/u);
  assert.equal((await verifyCancellation).lifecycle, "cancelled");
  assert.equal(verifications, 0);
  await verifyKernel.close();
});
