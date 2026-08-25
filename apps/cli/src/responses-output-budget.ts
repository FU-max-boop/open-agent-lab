const POLICIES = Object.freeze({
  scored_slot: Object.freeze({
    budgetClass: "scored_slot", accountingMode: "sealed_usage_debit", slotOutputTokenLimit: 50_000,
  } as const),
  zai_route_probe: Object.freeze({
    budgetClass: "zai_route_probe",
    accountingMode: "fixed_round_allocations",
    slotOutputTokenLimit: 8_448,
    roundAllocations: Object.freeze([8_192, 256] as const),
  } as const),
  unmetered_route_probe: Object.freeze({
    budgetClass: "unmetered_route_probe", accountingMode: "none", slotOutputTokenLimit: null,
  } as const),
});
export type OutputBudgetClass = keyof typeof POLICIES;
export type OutputBudgetPolicy = (typeof POLICIES)[OutputBudgetClass];
export type OutputBudgetAccountingState = "budget_terminal" | "complete" |
  "exact_exhaustion" | "poisoned" | "probe_conformant" | "unmetered";
export interface OutputBudgetTerminalObservation {
  terminalEvent: unknown; terminalStatus: unknown; incompleteReason: unknown; usage: unknown;
  metadataConflicts?: unknown;
}
type RejectionCode = "post_terminal_request" | "request_in_flight" |
  "request_ordinal_mismatch" | "slot_output_budget_exhausted" | "slot_poisoned";
export type OutputBudgetAdmission = {
  kind: "accepted";
  requestOrdinal: number;
  requestedMaxOutputTokens: number | null;
  effectiveMaxOutputTokens: number | null;
} | { kind: "rejected"; requestOrdinal: number; code: RejectionCode };
export type OutputBudgetAcceptedAdmission = Extract<OutputBudgetAdmission, { kind: "accepted" }>;
export type OutputBudgetRejectedAdmission = Extract<OutputBudgetAdmission, { kind: "rejected" }>;
export interface OutputBudgetSettlement {
  kind: "poisoned" | "settled"; requestOrdinal: number | null;
  state: Exclude<OutputBudgetAccountingState, "exact_exhaustion">; reason: string | null;
}
interface OutputTokenAccounting {
  state: OutputBudgetAccountingState; reportedOutputTokens: number | null;
  conservativeOutputTokenUpperBound: number | null; unusedOutputTokensBurned: number;
}
export interface OutputBudgetSeal {
  budgetClass: OutputBudgetClass;
  accountingMode: OutputBudgetPolicy["accountingMode"];
  slotOutputTokenLimit: number | null;
  outputTokenAccounting: OutputTokenAccounting;
}
export class OutputBudgetInputError extends Error {
  readonly code = "invalid_max_output_tokens" as const;
  constructor() {
    super("requestedMaxOutputTokens must be a positive safe integer or null.");
    this.name = "OutputBudgetInputError";
  }
}
export function outputBudgetPolicy(value: string): OutputBudgetPolicy {
  if (value === "scored_slot") return POLICIES.scored_slot;
  if (value === "zai_route_probe") return POLICIES.zai_route_probe;
  if (value === "unmetered_route_probe") return POLICIES.unmetered_route_probe;
  throw new Error(`unsupported output budget class: ${value}`);
}
function positiveInteger(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0)
    throw new Error(`${name} must be a positive safe integer.`);
  return value;
}
function requestedMaximum(value: unknown): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0)
    throw new OutputBudgetInputError();
  return value;
}
export function reportedOutputTokens(value: unknown): number | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const usage = value as Record<string, unknown>;
  const [input, output, total] = [usage.input_tokens, usage.output_tokens, usage.total_tokens];
  const valid = typeof input === "number" && Number.isSafeInteger(input) && input >= 0 &&
    typeof output === "number" && Number.isSafeInteger(output) && output >= 0 &&
    typeof total === "number" && Number.isSafeInteger(total) && total === input + output;
  return valid ? output : null;
}
export function outputBudgetTerminalKind(
  value: OutputBudgetTerminalObservation,
): "completed" | "max_output_tokens" | null {
  const status = value.terminalStatus;
  if (value.terminalEvent === "response.completed" && (status === null || status === "completed") &&
    value.incompleteReason === null) return "completed";
  if (value.terminalEvent === "response.incomplete" && (status === null || status === "incomplete") &&
    value.incompleteReason === "max_output_tokens") return "max_output_tokens";
  return null;
}
export class ResponsesOutputBudgetLedger {
  readonly policy: OutputBudgetPolicy;
  private nextOrdinal = 1;
  private pending: OutputBudgetAcceptedAdmission | null = null;
  private state: OutputBudgetAccountingState | null = null;
  private reason: string | null = null;
  private reported = 0;
  private remaining: number | null;
  private retired = 0;
  private probeRounds = 0;
  private poisonUpper: number | null = null;
  private closed = false;
  constructor(budgetClass: string) {
    this.policy = outputBudgetPolicy(budgetClass);
    this.remaining = this.policy.slotOutputTokenLimit;
  }
  admit(ordinalValue: unknown, requestedValue: unknown): OutputBudgetAdmission {
    const ordinal = positiveInteger(ordinalValue, "requestOrdinal");
    const requested = requestedMaximum(requestedValue);
    if (this.state === "poisoned") return this.rejection(ordinal, "slot_poisoned", false);
    if (ordinal !== this.nextOrdinal) return this.rejection(ordinal, "request_ordinal_mismatch");
    if (this.pending !== null) return this.rejection(ordinal, "request_in_flight");
    if (this.closed) return this.rejection(ordinal, "post_terminal_request");
    if (this.policy.budgetClass === "scored_slot" && this.remaining === 0) {
      this.nextOrdinal += 1;
      this.closed = true;
      this.state = "exact_exhaustion";
      return { kind: "rejected", requestOrdinal: ordinal, code: "slot_output_budget_exhausted" };
    }
    let effective: number | null = requested;
    if (this.policy.budgetClass === "scored_slot") {
      if (this.remaining === null || this.remaining <= 0) throw new Error("invalid scored budget");
      effective = Math.min(requested ?? this.remaining, this.remaining);
    } else if (this.policy.budgetClass === "zai_route_probe") {
      const allocation = this.policy.roundAllocations[this.probeRounds];
      if (allocation === undefined) return this.rejection(ordinal, "post_terminal_request");
      effective = allocation;
      this.retired += allocation;
      this.remaining = this.policy.slotOutputTokenLimit - this.retired;
    }
    const admission = {
      kind: "accepted" as const,
      requestOrdinal: ordinal,
      requestedMaxOutputTokens: requested,
      effectiveMaxOutputTokens: effective,
    };
    this.pending = admission;
    this.nextOrdinal += 1;
    return admission;
  }

  settle(observation: OutputBudgetTerminalObservation): OutputBudgetSettlement {
    const pending = this.pending;
    if (pending === null) return this.fail(null, "terminal_without_pending_request");
    const terminal = outputBudgetTerminalKind(observation);
    if (terminal === null) return this.fail(pending.requestOrdinal, "terminal_binding_invalid");
    if (
      Array.isArray(observation.metadataConflicts) &&
      observation.metadataConflicts.includes("usage")
    ) {
      return this.fail(pending.requestOrdinal, "usage_conflict");
    }
    const output = reportedOutputTokens(observation.usage);
    if (output === null) return this.fail(pending.requestOrdinal, "usage_incomplete");
    if (pending.effectiveMaxOutputTokens !== null && output > pending.effectiveMaxOutputTokens) {
      return this.fail(pending.requestOrdinal, "output_tokens_exceed_effective_max");
    }
    if (this.policy.budgetClass === "zai_route_probe") {
      const expected = this.probeRounds === 0 ? "completed" : "max_output_tokens";
      if (terminal !== expected) return this.fail(pending.requestOrdinal, "unexpected_probe_terminal");
    } else if (this.policy.budgetClass === "unmetered_route_probe" && terminal !== "completed") {
      return this.fail(pending.requestOrdinal, "unexpected_unmetered_terminal");
    }
    this.reported += output;
    this.pending = null;
    if (this.policy.budgetClass === "scored_slot") {
      if (this.remaining === null) return this.fail(pending.requestOrdinal, "scored_budget_inconsistent");
      this.remaining -= output;
      if (terminal === "completed") this.state = "complete";
      else {
        this.remaining = 0;
        this.closed = true;
        this.state = "budget_terminal";
      }
    } else if (this.policy.budgetClass === "zai_route_probe") {
      this.probeRounds += 1;
      this.state = this.probeRounds === 1 ? "complete" : "probe_conformant";
      if (this.probeRounds === 2) this.closed = true;
    } else {
      this.state = "unmetered";
    }
    return this.result(pending.requestOrdinal);
  }

  poison(reason: unknown): void {
    if (typeof reason !== "string" || !/^[a-z][a-z0-9_]{0,127}$/u.test(reason)) {
      throw new Error("poison reason must be a bounded snake-case code.");
    }
    this.poisonInternal(reason);
  }

  poisonBeforeUpstream(reason: unknown): void {
    if (this.pending === null) throw new Error("no pending output budget admission");
    this.pending = null;
    this.poison(reason);
  }

  seal(): OutputBudgetSeal {
    if (this.pending !== null) this.poisonInternal("unsettled_lifecycle");
    if (this.state === null) {
      if (this.policy.budgetClass === "unmetered_route_probe") this.state = "unmetered";
      else this.poisonInternal("no_accepted_lifecycle");
    }
    const state = this.state;
    if (state === null) throw new Error("output budget state was not sealed");
    const poisoned = state === "poisoned";
    const reported = poisoned ? null : this.reported;
    const upper = poisoned ? this.poisonUpper : this.reported;
    let unused = 0;
    if (this.policy.slotOutputTokenLimit !== null && upper !== null) {
      unused = state === "complete" && this.policy.budgetClass === "zai_route_probe"
        ? this.retired - this.reported
        : this.policy.slotOutputTokenLimit - upper;
    }
    return {
      budgetClass: this.policy.budgetClass,
      accountingMode: this.policy.accountingMode,
      slotOutputTokenLimit: this.policy.slotOutputTokenLimit,
      outputTokenAccounting: {
        state,
        reportedOutputTokens: reported,
        conservativeOutputTokenUpperBound: upper,
        unusedOutputTokensBurned: unused,
      },
    };
  }

  private rejection(requestOrdinal: number, code: RejectionCode, poison = true): OutputBudgetRejectedAdmission {
    if (poison) this.poisonInternal(code);
    if (code !== "slot_poisoned") this.nextOrdinal += 1;
    return { kind: "rejected", requestOrdinal, code };
  }

  private fail(requestOrdinal: number | null, reason: string): OutputBudgetSettlement {
    this.poisonInternal(reason);
    return this.result(requestOrdinal);
  }

  private result(requestOrdinal: number | null): OutputBudgetSettlement {
    const state = this.state;
    if (state === null || state === "exact_exhaustion") throw new Error("invalid settlement state");
    return {
      kind: state === "poisoned" ? "poisoned" : "settled",
      requestOrdinal,
      state,
      reason: state === "poisoned" ? this.reason : null,
    };
  }

  private poisonInternal(reason: string): void {
    if (this.state === "poisoned") return;
    const effective = this.pending?.effectiveMaxOutputTokens ?? 0;
    this.poisonUpper = this.policy.slotOutputTokenLimit === null
      ? null
      : this.reported + effective;
    this.pending = null;
    this.remaining = this.policy.slotOutputTokenLimit === null ? null : 0;
    this.closed = true;
    this.state = "poisoned";
    this.reason = reason;
  }
}
