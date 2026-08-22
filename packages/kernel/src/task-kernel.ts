import { canonicalJson, type JsonObject, type Sha256Digest } from "@open-agent-lab/contracts";
import {
  ToolBroker,
  ToolBrokerError,
  type ToolExecutionResult,
  type ToolInvocationRequest,
} from "@open-agent-lab/tool-broker";

import { DIGEST_PATTERN, kernelDigest } from "./digest.js";
import { KernelError } from "./errors.js";
import { initialState, reduceEvent } from "./state.js";
import {
  SqliteKernelStore,
  type SqliteKernelStoreCreateOptions,
  type SqliteKernelStoreOptions,
} from "./store.js";
import {
  KERNEL_EVENT_VERSION,
  type EventHead,
  type KernelEventBodyV1,
  type KernelEventV1,
  type KernelResumeResult,
  type KernelStateSnapshotV1,
  type ReviewDecision,
  type RunVerifier,
  type VerifierRecordV1,
} from "./types.js";

export interface TaskKernelCreateOptions extends SqliteKernelStoreCreateOptions {
  directory: string;
  runId: string;
  taskDigest: Sha256Digest;
  broker: ToolBroker;
}

export interface TaskKernelOpenOptions extends SqliteKernelStoreOptions {
  directory: string;
  runId: string;
  broker: ToolBroker;
}

function clone<T>(value: T): T {
  return JSON.parse(canonicalJson(value)) as T;
}

function data(value: unknown): JsonObject {
  return clone(value) as JsonObject;
}

export class TaskKernel {
  readonly #store: SqliteKernelStore;
  readonly #broker: ToolBroker;
  readonly #clock: () => string;
  #state: KernelStateSnapshotV1;
  #head: EventHead;
  #tail: Promise<void> = Promise.resolve();
  #closed = false;
  #activeAbort: AbortController | undefined;
  #cancelRequested = false;

  private constructor(
    store: SqliteKernelStore,
    broker: ToolBroker,
    state: KernelStateSnapshotV1,
    head: EventHead,
    clock: () => string,
  ) {
    this.#store = store;
    this.#broker = broker;
    this.#state = state;
    this.#head = head;
    this.#clock = clock;
  }

  static async create(options: TaskKernelCreateOptions): Promise<TaskKernel> {
    const clock = options.clock ?? (() => new Date().toISOString());
    const store = SqliteKernelStore.create(
      options.directory,
      options.runId,
      options.taskDigest,
      options,
    );
    const kernel = new TaskKernel(
      store,
      options.broker,
      initialState(options.runId, options.taskDigest),
      { sequence: -1, eventHash: null },
      clock,
    );
    try {
      kernel.#commit("run.created", { taskDigest: options.taskDigest });
      return kernel;
    } catch (error) {
      store.close();
      throw error;
    }
  }

  static async open(options: TaskKernelOpenOptions): Promise<TaskKernel> {
    const clock = options.clock ?? (() => new Date().toISOString());
    const store = SqliteKernelStore.open(options.directory, options.runId, options);
    try {
      const events = store.loadEvents();
      const emptyState = initialState(store.metadata.runId, store.metadata.taskDigest);
      if (events.length === 0) {
        if (store.initialized) {
          throw new KernelError("corrupt_journal", "Initialized kernel journal is empty.");
        }
        const kernel = new TaskKernel(
          store,
          options.broker,
          emptyState,
          { sequence: -1, eventHash: null },
          clock,
        );
        kernel.#commit("run.created", { taskDigest: store.metadata.taskDigest });
        store.resolveTakeover();
        return kernel;
      }
      if (!store.initialized) {
        throw new KernelError("corrupt_journal", "Uninitialized store contains journal events.");
      }
      const last = events.at(-1)!;
      const head = { sequence: last.sequence, eventHash: last.eventHash };
      let state = emptyState;
      for (const event of events) state = reduceEvent(state, event);
      store.syncCheckpoint(head, state);
      const kernel = new TaskKernel(
        store,
        options.broker,
        state,
        head,
        clock,
      );
      if (
        store.tookOverExpiredLease &&
        state.lifecycle === "running" &&
        state.pending !== undefined &&
        (state.pending.effect === "workspace_mutation" ||
          state.pending.effect === "external_non_idempotent")
      ) {
        kernel.#commit("review.required", {
          reason: "Previous writer lease expired; stop the old writer before resolving this effect.",
        });
      }
      store.resolveTakeover();
      return kernel;
    } catch (error) {
      store.close();
      throw error;
    }
  }

  get state(): Readonly<KernelStateSnapshotV1> {
    return clone(this.#state);
  }

  get journal(): readonly KernelEventV1[] {
    return this.#store.loadEvents();
  }

  async invoke(request: ToolInvocationRequest): Promise<ToolExecutionResult> {
    return this.#serial(async () => {
      this.#requireActive();
      if (this.#state.pending !== undefined) {
        throw new KernelError("invalid_state", "Resume the pending tool before invoking another.");
      }
      const invocation = await this.#leased((signal) =>
        this.#broker.prepare(request, { runId: this.#state.runId, signal }),
      );
      const seenFingerprint = Object.hasOwn(
        this.#state.invocationFingerprints,
        invocation.invocationId,
      ) ? this.#state.invocationFingerprints[invocation.invocationId] : undefined;
      if (seenFingerprint !== undefined && seenFingerprint !== invocation.actionFingerprint) {
        throw new KernelError("invocation_conflict", "Invocation ID identifies a different action.");
      }
      const safeAcrossIds = invocation.effect === "read_only" || invocation.effect === "idempotent";
      const reused = seenFingerprint === invocation.actionFingerprint || safeAcrossIds
        ? this.#state.completed.find(
            (entry) => entry.invocation.actionFingerprint === invocation.actionFingerprint,
          )
        : undefined;
      if (reused !== undefined) {
        this.#commit("tool.reused", { invocation });
        return clone(reused.result);
      }
      this.#commit("tool.intent", { invocation });
      return this.#executePending();
    });
  }

  async resume(): Promise<KernelResumeResult> {
    return this.#serial(async () => {
      this.#requireNotCancelling();
      if (this.#state.lifecycle === "needs_review") return this.#resumeResult("needs_review");
      if (this.#state.lifecycle !== "running" || this.#state.pending === undefined) {
        return this.#resumeResult("nothing_pending");
      }
      const pending = this.#state.pending;
      try {
        if (pending.effect === "external_non_idempotent") {
          return this.#requireReview("External non-idempotent effect may already have occurred.");
        }
        if (pending.effect === "workspace_mutation") {
          const outcome = await this.#leased((signal) =>
            this.#broker.reconcile(pending, { runId: this.#state.runId, signal }),
          );
          if (outcome.status === "applied") {
            this.#complete(pending.invocationId, outcome.result);
            return this.#resumeResult("reconciled", outcome.result);
          }
          if (outcome.status === "unknown") return this.#requireReview(outcome.reason);
        }
        const result = await this.#executePending();
        return this.#resumeResult("replayed", result);
      } catch (error) {
        if (error instanceof ToolBrokerError && [
          "contract_drift",
          "precondition_changed",
          "unknown_tool",
        ].includes(error.code)) {
          return this.#requireReview(`Cannot safely continue the pending tool: ${error.message}`);
        }
        throw error;
      }
    });
  }

  async resolveReview(decision: ReviewDecision): Promise<Readonly<KernelStateSnapshotV1>> {
    return this.#serial(async () => {
      this.#requireNotCancelling();
      if (this.#state.lifecycle !== "needs_review" || this.#state.pending === undefined) {
        throw new KernelError("invalid_state", "Run is not waiting for review.");
      }
      const invocationId = this.#state.pending.invocationId;
      if (typeof decision !== "object" || decision === null || !("action" in decision)) {
        throw new KernelError("invalid_state", "Review decision is invalid.");
      }
      if (decision.action === "confirmed_applied") {
        this.#commit("review.confirmed_applied", {
          invocationId,
          operator: decision.operator,
          reason: decision.reason,
          result: decision.result,
        });
      } else if (decision.action === "abort") {
        this.#commit("review.aborted", {
          invocationId,
          operator: decision.operator,
          reason: decision.reason,
        });
      } else if (decision.action === "confirmed_not_applied_then_retry") {
        this.#commit("review.retry_authorized", {
          invocationId,
          operator: decision.operator,
          reason: decision.reason,
        });
        await this.#executePending();
      } else {
        throw new KernelError("invalid_state", "Review decision action is invalid.");
      }
      return this.state;
    });
  }

  async verify(verifier: RunVerifier): Promise<VerifierRecordV1> {
    return this.#serial(async () => {
      this.#requireActive();
      if (this.#state.pending !== undefined) {
        throw new KernelError("invalid_state", "Resolve the pending tool before verification.");
      }
      const outcome = await this.#leased((signal) =>
        verifier.verify({
          runId: this.#state.runId,
          taskDigest: this.#state.taskDigest,
          signal,
        }),
      );
      if (!DIGEST_PATTERN.test(outcome.workspaceDigest) || typeof outcome.passed !== "boolean") {
        throw new KernelError("verifier_mismatch", "Verifier returned an invalid bound outcome.");
      }
      const record: VerifierRecordV1 = clone({
        runId: this.#state.runId,
        taskDigest: this.#state.taskDigest,
        workspaceDigest: outcome.workspaceDigest,
        verifierId: verifier.id,
        verifierVersion: verifier.version,
        passed: outcome.passed,
        ...(outcome.details === undefined ? {} : { details: outcome.details }),
      });
      this.#commit("verification.completed", { record });
      return record;
    });
  }

  async cancel(reason: string): Promise<Readonly<KernelStateSnapshotV1>> {
    if (typeof reason !== "string" || reason.trim() === "") {
      throw new KernelError("invalid_state", "Cancellation reason must be non-empty.");
    }
    this.#cancelRequested = true;
    this.#activeAbort?.abort();
    return this.#serial(async () => {
      try {
        if (this.#state.lifecycle === "cancelled") return this.state;
        if (
          this.#state.pending !== undefined &&
          this.#state.pending.effect !== "read_only"
        ) {
          if (this.#state.lifecycle === "running") {
            this.#commit("review.required", {
              reason: "Cancellation interrupted a pending effect; reconcile it before termination.",
            });
          }
          return this.state;
        }
        this.#commit("run.cancelled", { reason });
        return this.state;
      } finally {
        this.#cancelRequested = false;
      }
    });
  }

  async close(): Promise<void> {
    if (this.#closed) return;
    await this.#serial(async () => {
      this.#store.close();
      this.#closed = true;
    }, true);
  }

  #commit(type: string, value: unknown): void {
    const body: KernelEventBodyV1 = {
      schemaVersion: KERNEL_EVENT_VERSION,
      runId: this.#state.runId,
      sequence: this.#head.sequence + 1,
      timestamp: this.#clock(),
      type,
      data: data(value),
      previousEventHash: this.#head.eventHash,
    };
    const event: KernelEventV1 = { ...body, eventHash: kernelDigest(body) };
    const next = reduceEvent(this.#state, event); // Preflight before durable mutation.
    this.#head = this.#store.commit(this.#head, event, next);
    this.#state = next;
  }

  async #executePending(): Promise<ToolExecutionResult> {
    const pending = this.#state.pending;
    if (pending === undefined) throw new KernelError("invalid_state", "No tool is pending.");
    const result = await this.#leased((signal) =>
      this.#broker.execute(pending, { runId: this.#state.runId, signal }),
    );
    this.#complete(pending.invocationId, result);
    return clone(result);
  }

  #complete(invocationId: string, result: ToolExecutionResult): void {
    this.#commit("tool.completed", { invocationId, result });
  }

  #requireReview(reason: string): KernelResumeResult {
    this.#commit("review.required", { reason });
    return this.#resumeResult("needs_review");
  }

  #resumeResult(
    action: KernelResumeResult["action"],
    result?: ToolExecutionResult,
  ): KernelResumeResult {
    return { state: this.state, action, ...(result === undefined ? {} : { result: clone(result) }) };
  }

  #requireActive(): void {
    this.#requireNotCancelling();
    if (this.#state.lifecycle !== "running") {
      throw new KernelError("invalid_state", `Run is '${this.#state.lifecycle}', not running.`);
    }
  }

  async #leased<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
    this.#requireNotCancelling();
    this.#store.renewLease();
    const controller = new AbortController();
    this.#activeAbort = controller;
    let leaseError: unknown;
    const timer = setInterval(() => {
      try {
        this.#store.renewLease();
      } catch (error) {
        leaseError = error;
        controller.abort();
      }
    }, this.#store.heartbeatIntervalMs);
    timer.unref();
    try {
      const result = await operation(controller.signal);
      if (leaseError !== undefined) throw leaseError;
      if (controller.signal.aborted) {
        throw new KernelError("invalid_state", "Operation was cancelled.");
      }
      this.#store.renewLease();
      return result;
    } finally {
      clearInterval(timer);
      if (this.#activeAbort === controller) this.#activeAbort = undefined;
    }
  }

  #requireNotCancelling(): void {
    if (this.#cancelRequested) {
      throw new KernelError("invalid_state", "Cancellation was requested.");
    }
  }

  async #serial<T>(operation: () => Promise<T>, allowClosed = false): Promise<T> {
    let release = (): void => {};
    const previous = this.#tail;
    this.#tail = new Promise<void>((resolve) => { release = resolve; });
    await previous;
    try {
      if (this.#closed && !allowClosed) throw new KernelError("invalid_state", "Kernel is closed.");
      return await operation();
    } finally {
      release();
    }
  }
}
