export type ModelContractErrorCode =
  | "aborted"
  | "capability_mismatch"
  | "invalid_capabilities"
  | "invalid_driver"
  | "invalid_request"
  | "invalid_stream"
  | "script_exhausted";

/** A deterministic, provider-neutral failure at the model-driver boundary. */
export class ModelContractError extends Error {
  readonly code: ModelContractErrorCode;

  constructor(code: ModelContractErrorCode, message: string) {
    super(message);
    this.name = "ModelContractError";
    this.code = code;
  }
}
