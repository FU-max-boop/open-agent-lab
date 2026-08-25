import assert from "node:assert/strict";
import test from "node:test";

import {
  OutputBudgetInputError,
  outputBudgetPolicy,
  ResponsesOutputBudgetLedger,
  type OutputBudgetAcceptedAdmission,
  type OutputBudgetAdmission,
  type OutputBudgetRejectedAdmission,
  type OutputBudgetTerminalObservation,
} from "../src/responses-output-budget.js";

function accepted(admission: OutputBudgetAdmission): OutputBudgetAcceptedAdmission {
  assert.equal(admission.kind, "accepted");
  if (admission.kind !== "accepted") throw new Error("expected an accepted admission");
  return admission;
}

function rejected(admission: OutputBudgetAdmission): OutputBudgetRejectedAdmission {
  assert.equal(admission.kind, "rejected");
  if (admission.kind !== "rejected") throw new Error("expected a rejected admission");
  return admission;
}

function terminal(
  kind: "completed" | "incomplete",
  outputTokens: number,
): OutputBudgetTerminalObservation {
  return {
    terminalEvent: `response.${kind}`,
    terminalStatus: kind,
    incompleteReason: kind === "incomplete" ? "max_output_tokens" : null,
    usage: {
      input_tokens: 7,
      output_tokens: outputTokens,
      total_tokens: 7 + outputTokens,
    },
  };
}

test("the policy factory exposes only the three frozen policies", () => {
  assert.deepEqual(outputBudgetPolicy("scored_slot"), {
    budgetClass: "scored_slot",
    accountingMode: "sealed_usage_debit",
    slotOutputTokenLimit: 50_000,
  });
  assert.deepEqual(outputBudgetPolicy("zai_route_probe"), {
    budgetClass: "zai_route_probe",
    accountingMode: "fixed_round_allocations",
    slotOutputTokenLimit: 8_448,
    roundAllocations: [8_192, 256],
  });
  assert.deepEqual(outputBudgetPolicy("unmetered_route_probe"), {
    budgetClass: "unmetered_route_probe",
    accountingMode: "none",
    slotOutputTokenLimit: null,
  });
  assert.throws(() => outputBudgetPolicy("route_probe"), /unsupported output budget class/u);
});

test("sealing without an accepted lifecycle is deterministic and fail-closed", () => {
  for (const [budgetClass, limit] of [
    ["scored_slot", 50_000],
    ["zai_route_probe", 8_448],
  ] as const) {
    const ledger = new ResponsesOutputBudgetLedger(budgetClass);
    assert.deepEqual(ledger.seal().outputTokenAccounting, {
      state: "poisoned",
      reportedOutputTokens: null,
      conservativeOutputTokenUpperBound: 0,
      unusedOutputTokensBurned: limit,
    });
  }
  const unmetered = new ResponsesOutputBudgetLedger("unmetered_route_probe");
  assert.deepEqual(unmetered.seal().outputTokenAccounting, {
    state: "unmetered",
    reportedOutputTokens: 0,
    conservativeOutputTokenUpperBound: 0,
    unusedOutputTokensBurned: 0,
  });
});

test("a scored slot debits sealed usage and clamps its continuation to 20k", () => {
  const ledger = new ResponsesOutputBudgetLedger("scored_slot");
  assert.deepEqual(ledger.admit(1, null), {
    kind: "accepted",
    requestOrdinal: 1,
    requestedMaxOutputTokens: null,
    effectiveMaxOutputTokens: 50_000,
  });
  assert.equal(ledger.settle(terminal("completed", 30_000)).state, "complete");
  assert.deepEqual(ledger.admit(2, null), {
    kind: "accepted",
    requestOrdinal: 2,
    requestedMaxOutputTokens: null,
    effectiveMaxOutputTokens: 20_000,
  });
});

test("a final complete scored seal burns its non-transferable unused slot budget", () => {
  const ledger = new ResponsesOutputBudgetLedger("scored_slot");
  ledger.admit(1, null);
  ledger.settle(terminal("completed", 30_000));
  assert.deepEqual(ledger.seal().outputTokenAccounting, {
    state: "complete",
    reportedOutputTokens: 30_000,
    conservativeOutputTokenUpperBound: 30_000,
    unusedOutputTokensBurned: 20_000,
  });
});

test("a scored incomplete max-output terminal burns the remaining slot budget", () => {
  const ledger = new ResponsesOutputBudgetLedger("scored_slot");
  ledger.admit(1, null);
  ledger.settle(terminal("completed", 30_000));
  ledger.admit(2, 10_000);
  assert.equal(ledger.settle(terminal("incomplete", 9_000)).state, "budget_terminal");
  assert.deepEqual(ledger.seal(), {
    budgetClass: "scored_slot",
    accountingMode: "sealed_usage_debit",
    slotOutputTokenLimit: 50_000,
    outputTokenAccounting: {
      state: "budget_terminal",
      reportedOutputTokens: 39_000,
      conservativeOutputTokenUpperBound: 39_000,
      unusedOutputTokensBurned: 11_000,
    },
  });
});

test("exactly exhausting a scored slot gives the dedicated local rejection", () => {
  const ledger = new ResponsesOutputBudgetLedger("scored_slot");
  ledger.admit(1, null);
  ledger.settle(terminal("completed", 50_000));
  assert.equal(ledger.seal().outputTokenAccounting.state, "complete");
  assert.deepEqual(ledger.admit(2, null), {
    kind: "rejected",
    requestOrdinal: 2,
    code: "slot_output_budget_exhausted",
  });
  assert.deepEqual(ledger.seal().outputTokenAccounting, {
    state: "exact_exhaustion",
    reportedOutputTokens: 50_000,
    conservativeOutputTokenUpperBound: 50_000,
    unusedOutputTokensBurned: 0,
  });
});

test("unknown or over-effective settlement poisons with a conservative reservation", () => {
  for (const observation of [
    { ...terminal("completed", 1), usage: null },
    { ...terminal("completed", 1), usage: { output_tokens: 1 } },
    { ...terminal("completed", 1), metadataConflicts: ["usage"] },
    terminal("completed", 10_001),
    {
      ...terminal("completed", 1),
      terminalStatus: "failed",
    },
    {
      ...terminal("incomplete", 1),
      incompleteReason: "content_filter",
    },
  ]) {
    const ledger = new ResponsesOutputBudgetLedger("scored_slot");
    ledger.admit(1, 10_000);
    assert.equal(ledger.settle(observation).state, "poisoned");
    assert.deepEqual(ledger.seal().outputTokenAccounting, {
      state: "poisoned",
      reportedOutputTokens: null,
      conservativeOutputTokenUpperBound: 10_000,
      unusedOutputTokensBurned: 40_000,
    });
  }
});

test("an explicit transport poison preserves prior usage only as a conservative upper bound", () => {
  const ledger = new ResponsesOutputBudgetLedger("scored_slot");
  ledger.admit(1, null);
  ledger.settle(terminal("completed", 30_000));
  ledger.admit(2, 10_000);
  ledger.poison("transport_interrupted");
  assert.deepEqual(ledger.seal().outputTokenAccounting, {
    state: "poisoned",
    reportedOutputTokens: null,
    conservativeOutputTokenUpperBound: 40_000,
    unusedOutputTokensBurned: 10_000,
  });
});

test("ZAI fixed allocations retire 8192 then 256 without transferring unused budget", () => {
  const ledger = new ResponsesOutputBudgetLedger("zai_route_probe");
  assert.equal(accepted(ledger.admit(1, null)).effectiveMaxOutputTokens, 8_192);
  ledger.settle({ ...terminal("completed", 1), terminalStatus: null });
  assert.deepEqual(ledger.seal().outputTokenAccounting, {
    state: "complete",
    reportedOutputTokens: 1,
    conservativeOutputTokenUpperBound: 1,
    unusedOutputTokensBurned: 8_191,
  });
  assert.equal(accepted(ledger.admit(2, 9_999)).effectiveMaxOutputTokens, 256);
  assert.equal(
    ledger.settle({ ...terminal("incomplete", 200), terminalStatus: null }).state,
    "probe_conformant",
  );
  assert.deepEqual(ledger.seal().outputTokenAccounting, {
    state: "probe_conformant",
    reportedOutputTokens: 201,
    conservativeOutputTokenUpperBound: 201,
    unusedOutputTokensBurned: 8_247,
  });
  assert.deepEqual(ledger.admit(3, null), {
    kind: "rejected",
    requestOrdinal: 3,
    code: "post_terminal_request",
  });
  assert.equal(ledger.seal().outputTokenAccounting.state, "poisoned");
});

test("ZAI requires completed round one and max-output incomplete round two", () => {
  const firstRound = new ResponsesOutputBudgetLedger("zai_route_probe");
  firstRound.admit(1, null);
  assert.equal(firstRound.settle(terminal("incomplete", 1)).state, "poisoned");

  const secondRound = new ResponsesOutputBudgetLedger("zai_route_probe");
  secondRound.admit(1, null);
  secondRound.settle(terminal("completed", 1));
  secondRound.admit(2, null);
  assert.equal(secondRound.settle(terminal("completed", 1)).state, "poisoned");
});

test("unmetered probes preserve a client maximum without relay injection", () => {
  const ledger = new ResponsesOutputBudgetLedger("unmetered_route_probe");
  assert.equal(accepted(ledger.admit(1, 123)).effectiveMaxOutputTokens, 123);
  assert.equal(ledger.settle(terminal("completed", 5)).state, "unmetered");
  assert.deepEqual(ledger.seal().outputTokenAccounting, {
    state: "unmetered",
    reportedOutputTokens: 5,
    conservativeOutputTokenUpperBound: 5,
    unusedOutputTokensBurned: 0,
  });
});

test("admission rejects boolean, fractional, non-positive, and unsafe maxima", () => {
  for (const invalid of [undefined, false, true, 0, -1, 1.5, Number.NaN, Infinity, 2 ** 53]) {
    const ledger = new ResponsesOutputBudgetLedger("scored_slot");
    assert.throws(
      () => ledger.admit(1, invalid),
      /requestedMaxOutputTokens must be a positive safe integer/u,
    );
  }
  try {
    new ResponsesOutputBudgetLedger("scored_slot").admit(1, false);
    assert.fail("invalid maximum was accepted");
  } catch (error) {
    assert.ok(error instanceof OutputBudgetInputError);
    assert.equal(error.code, "invalid_max_output_tokens");
  }
});

test("ordinal and in-flight conflicts poison without an upstream admission", () => {
  const ordinal = new ResponsesOutputBudgetLedger("scored_slot");
  assert.equal(rejected(ordinal.admit(2, null)).code, "request_ordinal_mismatch");
  assert.equal(ordinal.seal().outputTokenAccounting.state, "poisoned");

  const concurrent = new ResponsesOutputBudgetLedger("scored_slot");
  concurrent.admit(1, null);
  assert.equal(rejected(concurrent.admit(2, null)).code, "request_in_flight");
  assert.deepEqual(concurrent.seal().outputTokenAccounting, {
    state: "poisoned",
    reportedOutputTokens: null,
    conservativeOutputTokenUpperBound: 50_000,
    unusedOutputTokensBurned: 0,
  });
});
