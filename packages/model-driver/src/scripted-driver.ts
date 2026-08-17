import {
  assertRequestSupported,
  parseModelCapabilities,
} from "./capabilities.js";
import { ModelContractError } from "./errors.js";
import type {
  ModelCallOptions,
  ModelCapabilities,
  ModelDriver,
  ModelProbeOptions,
  ModelRequest,
  ModelStreamEvent,
} from "./types.js";

export interface ScriptedModelTurn {
  events: readonly ModelStreamEvent[];
}

export interface ScriptedModelDriverOptions {
  driverId?: string;
  capabilities: ModelCapabilities;
  turns: readonly ScriptedModelTurn[];
}

function throwIfAborted(signal: AbortSignal | undefined, operation: string): void {
  if (signal?.aborted === true) {
    throw new ModelContractError("aborted", `${operation} was aborted.`);
  }
}

/**
 * Offline driver for contract tests and deterministic smoke runs. It applies
 * the same capability checks as a live adapter and never performs I/O.
 */
export class ScriptedModelDriver implements ModelDriver {
  readonly driverId: string;
  readonly #capabilities: Readonly<ModelCapabilities>;
  readonly #turns: readonly ScriptedModelTurn[];
  readonly #requests: ModelRequest[] = [];
  #cursor = 0;
  #probeCount = 0;

  constructor(options: ScriptedModelDriverOptions) {
    this.driverId = options.driverId ?? "scripted";
    this.#capabilities = parseModelCapabilities(options.capabilities);
    this.#turns = structuredClone(options.turns);
  }

  get probeCount(): number {
    return this.#probeCount;
  }

  get consumedTurns(): number {
    return this.#cursor;
  }

  get remainingTurns(): number {
    return this.#turns.length - this.#cursor;
  }

  get requests(): readonly ModelRequest[] {
    return Object.freeze(structuredClone(this.#requests));
  }

  async probe(options: ModelProbeOptions = {}): Promise<ModelCapabilities> {
    throwIfAborted(options.signal, "Capability probe");
    this.#probeCount += 1;
    return structuredClone(this.#capabilities);
  }

  async *stream(
    request: ModelRequest,
    options: ModelCallOptions = {},
  ): AsyncIterable<ModelStreamEvent> {
    throwIfAborted(options.signal, "Model stream");
    assertRequestSupported(request, this.#capabilities);

    const turn = this.#turns[this.#cursor];
    if (turn === undefined) {
      throw new ModelContractError(
        "script_exhausted",
        `Scripted model has no turn at index ${this.#cursor}.`,
      );
    }

    this.#cursor += 1;
    this.#requests.push(structuredClone(request));
    for (const event of turn.events) {
      throwIfAborted(options.signal, "Model stream");
      yield structuredClone(event);
    }
  }

  assertExhausted(): void {
    if (this.remainingTurns !== 0) {
      throw new ModelContractError(
        "script_exhausted",
        `Scripted model still has ${this.remainingTurns} unconsumed turn(s).`,
      );
    }
  }
}
