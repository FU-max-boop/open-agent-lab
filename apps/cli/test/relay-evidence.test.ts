import assert from "node:assert/strict";
import test from "node:test";

import { canonicalJson } from "@open-agent-lab/contracts";
import { sha256 } from "@open-agent-lab/evidence";

import { RELAY_VERSION, verifyRelayJournal, verifyRelaySeal } from "../src/relay-evidence.js";

const HASH = `sha256:${"0".repeat(64)}`;
const MODEL = "glm-5.3";
const RELAY_INSTANCE_ID = "00000000-0000-4000-8000-000000000001";

interface Lifecycle {
  requestedMax?: number | null;
  effectiveMax?: number | null;
  terminalEvent?: "response.completed" | "response.failed" | "response.incomplete" | null;
  terminalStatus?: "completed" | "failed" | "incomplete" | null;
  incompleteReason?: string | null;
  outputTokens?: number | null;
  transportState?: "completed" | "failed";
  usage?: Record<string, unknown> | null;
}

function journal(lifecycles: Lifecycle[]): string {
  const records: Record<string, unknown>[] = [];
  for (const [index, lifecycle] of lifecycles.entries()) {
    const ordinal = index + 1;
    const relayRequestId = `00000000-0000-4000-8000-${ordinal.toString().padStart(12, "0")}`;
    const transportState = lifecycle.transportState ?? "completed";
    const terminalEvent =
      lifecycle.terminalEvent === undefined ? "response.completed" : lifecycle.terminalEvent;
    const outputTokens = lifecycle.outputTokens === undefined ? 12 : lifecycle.outputTokens;
    const common = {
      schemaVersion: 2,
      relayVersion: RELAY_VERSION,
      runId: "relay-evidence-test",
      relayInstanceId: RELAY_INSTANCE_ID,
      providerId: "zai",
      buildId: "development",
      ordinal,
      relayRequestId,
      at: `2026-08-25T00:00:0${index * 3}.000Z`,
    };
    records.push(
      {
        ...common,
        event: "transport.responses.request",
        requestedModel: MODEL,
        requestBytes: 128,
        requestSha256: HASH,
        clientRequestId: null,
        stream: true,
        requestedMaxOutputTokens: lifecycle.requestedMax ?? null,
        effectiveMaxOutputTokens:
          lifecycle.effectiveMax === undefined ? 50_000 : lifecycle.effectiveMax,
      },
      {
        ...common,
        event: "transport.responses.headers",
        at: `2026-08-25T00:00:0${index * 3 + 1}.000Z`,
        status: transportState === "failed" ? null : 200,
        providerRequestId: transportState === "failed" ? null : `provider-${ordinal}`,
        modelHeader: null,
        headersMs: transportState === "failed" ? null : 1,
      },
      {
        ...common,
        event: "transport.responses.closed",
        at: `2026-08-25T00:00:0${index * 3 + 2}.000Z`,
        transportState,
        errorCategory: transportState === "failed" ? "upstream_failure" : null,
        status: transportState === "failed" ? null : 200,
        providerRequestId: transportState === "failed" ? null : `provider-${ordinal}`,
        responseBytes: transportState === "failed" ? 0 : 256,
        responseSha256: HASH,
        durationMs: 2,
        firstByteMs: transportState === "failed" ? null : 1,
        responseId: transportState === "failed" ? null : `resp-${ordinal}`,
        returnedModel: transportState === "failed" ? null : MODEL,
        modelConsistency: transportState === "failed" ? "missing" : "consistent",
        modelSources: {},
        systemFingerprint: null,
        terminalEvent,
        terminalStatus:
          lifecycle.terminalStatus === undefined
            ? terminalEvent?.replace("response.", "") ?? null
            : lifecycle.terminalStatus,
        incompleteReason:
          lifecycle.incompleteReason === undefined
            ? terminalEvent === "response.incomplete"
              ? "max_output_tokens"
              : null
            : lifecycle.incompleteReason,
        usage: lifecycle.usage === undefined
          ? outputTokens === null
            ? null
            : { input_tokens: 7, output_tokens: outputTokens, total_tokens: 7 + outputTokens }
          : lifecycle.usage,
        metadataConflicts: [],
        parseErrors: 0,
      },
    );
  }
  return rechain(records);
}

function rechain(records: Record<string, unknown>[]): string {
  if (records.length === 0) return "";
  let previous: string | null = null;
  return `${records
    .map((record) => {
      const body: Record<string, unknown> = { ...record, previousEventSha256: previous };
      delete body.eventSha256;
      const eventSha256 = sha256(canonicalJson(body));
      previous = eventSha256;
      return canonicalJson({ ...body, eventSha256 });
    })
    .join("\n")}\n`;
}

function parsed(journalContent: string): Record<string, unknown>[] {
  return journalContent
    .trimEnd()
    .split("\n")
    .map((line) => JSON.parse(line) as Record<string, unknown>);
}

function marker(
  journalContent: string,
  overrides: Record<string, unknown> = {},
): string {
  const summary = verifyRelayJournal(journalContent);
  const body = {
    schemaVersion: 2,
    state: "sealed",
    relayVersion: RELAY_VERSION,
    runId: "relay-evidence-test",
    relayInstanceId: RELAY_INSTANCE_ID,
    providerId: "zai",
    buildId: "development",
    expectedModel: MODEL,
    sealedAt: "2026-08-25T00:01:00.000Z",
    rejectedRequests: {},
    budgetClass: "scored_slot",
    accountingMode: "sealed_usage_debit",
    slotOutputTokenLimit: 50_000,
    outputTokenAccounting: {
      state: "complete",
      reportedOutputTokens: 12,
      conservativeOutputTokenUpperBound: 12,
      unusedOutputTokensBurned: 49_988,
    },
    eventCount: summary.eventCount,
    chainHead: summary.chainHead,
    ...overrides,
  };
  return `${canonicalJson({ ...body, markerSha256: sha256(canonicalJson(body)) })}\n`;
}

function tokens(
  state: string,
  reported: number | null,
  burned: number,
  upper: number | null = reported,
): Record<string, unknown> {
  return {
    state,
    reportedOutputTokens: reported,
    conservativeOutputTokenUpperBound: upper,
    unusedOutputTokensBurned: burned,
  };
}

function budgetMarker(
  journalContent: string,
  accounting: Record<string, unknown>,
  budgetClass: "scored_slot" | "zai_route_probe" | "unmetered_route_probe" = "scored_slot",
  overrides: Record<string, unknown> = {},
): string {
  const policy = {
    scored_slot: ["sealed_usage_debit", 50_000],
    zai_route_probe: ["fixed_round_allocations", 8_448],
    unmetered_route_probe: ["none", null],
  } as const;
  return marker(journalContent, {
    budgetClass,
    accountingMode: policy[budgetClass][0],
    slotOutputTokenLimit: policy[budgetClass][1],
    outputTokenAccounting: accounting,
    ...overrides,
  });
}

function rejectsSeal(journalContent: string, markerContent: string): void {
  assert.throws(() => verifyRelaySeal(journalContent, markerContent), /marker/u);
}

test("relay journal v2 binds request maxima and terminal status/reason", () => {
  const valid = journal([{}]);
  assert.equal(verifyRelayJournal(valid).eventCount, 3);
  assert.equal(verifyRelayJournal(journal([{ terminalStatus: null }])).eventCount, 3);
  const failed = journal([
    { terminalEvent: "response.failed", terminalStatus: null, outputTokens: 0 },
  ]);
  assert.equal(verifyRelayJournal(failed).eventCount, 3);

  const incomplete = journal([
    {
      requestedMax: 1_024,
      effectiveMax: 256,
      terminalEvent: "response.incomplete",
      terminalStatus: null,
      incompleteReason: "max_output_tokens",
    },
  ]);
  assert.equal(verifyRelayJournal(incomplete).eventCount, 3);

  for (const [field, value] of [
    ["requestedMaxOutputTokens", 0],
    ["requestedMaxOutputTokens", false],
    ["requestedMaxOutputTokens", 1.5],
    ["effectiveMaxOutputTokens", 0],
    ["effectiveMaxOutputTokens", false],
  ] as const) {
    const records = parsed(incomplete);
    records[0] = { ...records[0], [field]: value };
    assert.throws(() => verifyRelayJournal(rechain(records)), /record at line 1/u);
  }

  for (const mutation of [
    { terminalEvent: null, terminalStatus: "completed", incompleteReason: null },
    { terminalEvent: "response.completed", terminalStatus: "failed", incompleteReason: null },
    {
      terminalEvent: "response.completed",
      terminalStatus: "completed",
      incompleteReason: "max_output_tokens",
    },
    { terminalEvent: "response.incomplete", terminalStatus: "incomplete", incompleteReason: null },
    {
      terminalEvent: "response.incomplete",
      terminalStatus: "incomplete",
      incompleteReason: "x".repeat(513),
    },
  ]) {
    const records = parsed(valid);
    records[2] = { ...records[2], ...mutation };
    assert.throws(() => verifyRelayJournal(rechain(records)), /record at line 3/u);
  }

  const v1 = parsed(valid).map((record) => ({
    ...record,
    schemaVersion: 1,
    relayVersion: "native-responses-relay-v1",
  }));
  assert.throws(() => verifyRelayJournal(rechain(v1)), /record at line 1/u);
});

test("relay seal v2 binds exact budget policy and token accounting", () => {
  const complete = journal([{}]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(complete, budgetMarker(complete, tokens("complete", 12, 49_988))),
  );
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      complete,
      budgetMarker(complete, tokens("complete", 12, 49_988), "scored_slot", {
        rejectedRequests: { invalid_max_output_tokens: 1 },
      }),
    ),
  );

  const continued = journal([
    { outputTokens: 10 },
    { effectiveMax: 49_990, outputTokens: 20 },
  ]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(continued, budgetMarker(continued, tokens("complete", 30, 49_970))),
  );
  const continuedAfterTerminal = journal([
    {
      terminalEvent: "response.incomplete",
      incompleteReason: "max_output_tokens",
      outputTokens: 10,
    },
    { effectiveMax: 49_990, outputTokens: 20 },
  ]);
  rejectsSeal(
    continuedAfterTerminal,
    budgetMarker(continuedAfterTerminal, tokens("complete", 30, 49_970)),
  );

  const terminal = journal([
    { terminalEvent: "response.incomplete", incompleteReason: "max_output_tokens" },
  ]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      terminal,
      budgetMarker(terminal, tokens("budget_terminal", 12, 49_988)),
    ),
  );

  const exhausted = journal([{ outputTokens: 50_000 }]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      exhausted,
      budgetMarker(exhausted, tokens("exact_exhaustion", 50_000, 0), "scored_slot", {
        rejectedRequests: { slot_output_budget_exhausted: 1 },
      }),
    ),
  );

  const poisoned = journal([
    {
      terminalEvent: null,
      terminalStatus: null,
      incompleteReason: null,
      outputTokens: null,
      transportState: "failed",
    },
  ]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(poisoned, budgetMarker(poisoned, tokens("poisoned", null, 0, 50_000))),
  );

  const probe = journal([
    { requestedMax: 1, effectiveMax: 8_192, outputTokens: 100 },
    {
      requestedMax: 1,
      effectiveMax: 256,
      terminalEvent: "response.incomplete",
      incompleteReason: "max_output_tokens",
      outputTokens: 12,
    },
  ]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      probe,
      budgetMarker(probe, tokens("probe_conformant", 112, 8_336), "zai_route_probe"),
    ),
  );

  const probeOverEffective = journal([
    { requestedMax: 1, effectiveMax: 8_192, outputTokens: 100 },
    {
      requestedMax: 1,
      effectiveMax: 256,
      terminalEvent: "response.incomplete",
      incompleteReason: "max_output_tokens",
      outputTokens: 257,
    },
  ]);
  assert.equal(verifyRelayJournal(probeOverEffective).eventCount, 6);
  rejectsSeal(
    probeOverEffective,
    budgetMarker(
      probeOverEffective,
      tokens("probe_conformant", 357, 8_091),
      "zai_route_probe",
    ),
  );

  const probeWithThirdLifecycle = journal([
    { requestedMax: 1, effectiveMax: 8_192, outputTokens: 100 },
    {
      requestedMax: 1,
      effectiveMax: 256,
      terminalEvent: "response.incomplete",
      incompleteReason: "max_output_tokens",
      outputTokens: 12,
    },
    { requestedMax: 1, effectiveMax: 1, outputTokens: 1 },
  ]);
  assert.equal(verifyRelayJournal(probeWithThirdLifecycle).eventCount, 9);
  rejectsSeal(
    probeWithThirdLifecycle,
    budgetMarker(
      probeWithThirdLifecycle,
      tokens("probe_conformant", 113, 8_335),
      "zai_route_probe",
    ),
  );

  const incompleteUsage = journal([{ usage: { output_tokens: 12 } }]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      incompleteUsage,
      budgetMarker(incompleteUsage, tokens("poisoned", null, 0, 50_000)),
    ),
  );

  const probeRoundOne = journal([{ effectiveMax: 8_192, outputTokens: 100 }]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      probeRoundOne,
      budgetMarker(probeRoundOne, tokens("complete", 100, 8_092), "zai_route_probe"),
    ),
  );

  const unmetered = journal([{ effectiveMax: null }]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      unmetered,
      budgetMarker(unmetered, tokens("unmetered", 12, 0), "unmetered_route_probe"),
    ),
  );
  rejectsSeal(
    unmetered,
    budgetMarker(unmetered, tokens("unmetered", null, 0), "unmetered_route_probe"),
  );
  const unknownUnmetered = journal([{ effectiveMax: null, outputTokens: null }]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      unknownUnmetered,
      budgetMarker(
        unknownUnmetered,
        tokens("poisoned", null, 0),
        "unmetered_route_probe",
      ),
    ),
  );

  const empty = journal([]);
  assert.doesNotThrow(() =>
    verifyRelaySeal(empty, budgetMarker(empty, tokens("poisoned", null, 50_000, 0))),
  );
  assert.doesNotThrow(() =>
    verifyRelaySeal(
      empty,
      budgetMarker(empty, tokens("unmetered", 0, 0), "unmetered_route_probe"),
    ),
  );
  rejectsSeal(
    empty,
    budgetMarker(empty, tokens("poisoned", null, 0), "unmetered_route_probe"),
  );

  const extraAccounting = { ...tokens("complete", 12, 49_988), extra: 1 };
  for (const invalid of [
    marker(complete, { schemaVersion: 1, relayVersion: "native-responses-relay-v1" }),
    marker(complete, { accountingMode: "none" }),
    marker(complete, { slotOutputTokenLimit: 49_999 }),
    marker(complete, { budgetClass: "unknown" }),
    marker(complete, { terminalStatus: "completed" }),
    budgetMarker(complete, tokens("complete", 12, 49_988, 13)),
    budgetMarker(complete, tokens("poisoned", 0, 0, 50_000)),
    budgetMarker(complete, extraAccounting),
  ]) {
    rejectsSeal(complete, invalid);
  }

  rejectsSeal(
    exhausted,
    budgetMarker(exhausted, tokens("exact_exhaustion", 50_000, 0)),
  );

  const wrongDebit = parsed(continued);
  wrongDebit[3] = { ...wrongDebit[3], effectiveMaxOutputTokens: 50_000 };
  const wrongDebitJournal = rechain(wrongDebit);
  rejectsSeal(
    wrongDebitJournal,
    budgetMarker(wrongDebitJournal, tokens("complete", 30, 49_970)),
  );
});
