import { ModelContractError } from "./errors.js";
import type {
  ModelCapabilities,
  ModelCapabilityRequirements,
  ModelDriver,
  ModelProbeOptions,
  ModelRequest,
  StartedModelDriver,
} from "./types.js";

const BOOLEAN_CAPABILITIES = [
  "text",
  "image",
  "tools",
  "parallelTools",
  "strictSchema",
  "reasoning",
] as const;

type BooleanCapability = (typeof BOOLEAN_CAPABILITIES)[number];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requirePositiveSafeInteger(
  value: unknown,
  field: "context" | "output",
): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) {
    throw new ModelContractError(
      "invalid_capabilities",
      `Capability '${field}' must be a positive safe integer.`,
    );
  }
  return value as number;
}

/** Parse and freeze an untrusted capability probe result. */
export function parseModelCapabilities(
  value: unknown,
): Readonly<ModelCapabilities> {
  if (!isRecord(value)) {
    throw new ModelContractError(
      "invalid_capabilities",
      "Capability probe must return an object.",
    );
  }

  for (const key of BOOLEAN_CAPABILITIES) {
    if (typeof value[key] !== "boolean") {
      throw new ModelContractError(
        "invalid_capabilities",
        `Capability '${key}' must be a boolean.`,
      );
    }
  }

  const context = requirePositiveSafeInteger(value.context, "context");
  const output = requirePositiveSafeInteger(value.output, "output");

  if (output > context) {
    throw new ModelContractError(
      "invalid_capabilities",
      "Capability 'output' cannot exceed 'context'.",
    );
  }
  if (value.parallelTools === true && value.tools !== true) {
    throw new ModelContractError(
      "invalid_capabilities",
      "Capability 'parallelTools' requires 'tools'.",
    );
  }

  return Object.freeze({
    text: value.text as boolean,
    image: value.image as boolean,
    tools: value.tools as boolean,
    parallelTools: value.parallelTools as boolean,
    strictSchema: value.strictSchema as boolean,
    reasoning: value.reasoning as boolean,
    context,
    output,
  });
}

function requirePositiveRequirement(
  value: number | undefined,
  field: "minContext" | "minOutput",
): void {
  if (value !== undefined && (!Number.isSafeInteger(value) || value <= 0)) {
    throw new ModelContractError(
      "invalid_request",
      `Capability requirement '${field}' must be a positive safe integer.`,
    );
  }
}

/** Fail before a run starts when its declared needs cannot be satisfied. */
export function assertCapabilitiesSatisfy(
  capabilities: Readonly<ModelCapabilities>,
  requirements: ModelCapabilityRequirements,
): void {
  requirePositiveRequirement(requirements.minContext, "minContext");
  requirePositiveRequirement(requirements.minOutput, "minOutput");

  for (const key of BOOLEAN_CAPABILITIES) {
    if (requirements[key] === true && !capabilities[key]) {
      throw new ModelContractError(
        "capability_mismatch",
        `Model driver does not provide required capability '${key}'.`,
      );
    }
  }

  if (
    requirements.minContext !== undefined &&
    capabilities.context < requirements.minContext
  ) {
    throw new ModelContractError(
      "capability_mismatch",
      `Model context ${capabilities.context} is below required ${requirements.minContext}.`,
    );
  }
  if (
    requirements.minOutput !== undefined &&
    capabilities.output < requirements.minOutput
  ) {
    throw new ModelContractError(
      "capability_mismatch",
      `Model output ${capabilities.output} is below required ${requirements.minOutput}.`,
    );
  }
}

/** Derive capability requirements from request structure, not model identity. */
export function requirementsForRequest(
  request: ModelRequest,
): ModelCapabilityRequirements {
  let text = false;
  let image = false;
  let tools = (request.tools?.length ?? 0) > 0;

  for (const message of request.messages) {
    for (const part of message.content) {
      if (part.type === "text") text = true;
      if (part.type === "image") image = true;
      if (part.type === "tool_call" || part.type === "tool_result") tools = true;
    }
  }

  const strictSchema =
    request.responseSchema?.strict === true ||
    request.tools?.some((tool) => tool.strict === true) === true;
  const requestedOutput = request.generation?.maxOutputTokens;

  return {
    ...(text ? { text: true as const } : {}),
    ...(image ? { image: true as const } : {}),
    ...(tools ? { tools: true as const } : {}),
    ...(request.parallelToolCalls === true
      ? { parallelTools: true as const }
      : {}),
    ...(strictSchema ? { strictSchema: true as const } : {}),
    ...(request.reasoning?.enabled === true
      ? { reasoning: true as const }
      : {}),
    ...(requestedOutput === undefined ? {} : { minOutput: requestedOutput }),
  };
}

export function assertRequestSupported(
  request: ModelRequest,
  capabilities: Readonly<ModelCapabilities>,
): void {
  if (!Array.isArray(request.messages) || request.messages.length === 0) {
    throw new ModelContractError(
      "invalid_request",
      "A model request must contain at least one message.",
    );
  }
  if (
    request.parallelToolCalls === true &&
    (request.tools === undefined || request.tools.length === 0)
  ) {
    throw new ModelContractError(
      "invalid_request",
      "parallelToolCalls requires at least one tool definition.",
    );
  }
  const toolChoice = request.toolChoice;
  if (
    typeof toolChoice === "object" &&
    !request.tools?.some((tool) => tool.name === toolChoice.name)
  ) {
    throw new ModelContractError(
      "invalid_request",
      `toolChoice names undefined tool '${toolChoice.name}'.`,
    );
  }

  assertCapabilitiesSatisfy(capabilities, requirementsForRequest(request));
}

function throwIfProbeAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted === true) {
    throw new ModelContractError("aborted", "Capability probe was aborted.");
  }
}

/** Probe and validate once during runtime startup, before scheduling any work. */
export async function startModelDriver(
  driver: ModelDriver,
  requirements: ModelCapabilityRequirements = {},
  options: ModelProbeOptions = {},
): Promise<StartedModelDriver> {
  if (typeof driver.driverId !== "string" || driver.driverId.trim() === "") {
    throw new ModelContractError(
      "invalid_driver",
      "Model driver must expose a non-empty driverId.",
    );
  }
  throwIfProbeAborted(options.signal);

  const probed = await driver.probe(options);

  throwIfProbeAborted(options.signal);

  const capabilities = parseModelCapabilities(probed);
  assertCapabilitiesSatisfy(capabilities, requirements);
  return Object.freeze({ driver, capabilities });
}

export type { BooleanCapability };
