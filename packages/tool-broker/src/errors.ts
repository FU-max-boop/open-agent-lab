export type ToolBrokerErrorCode =
  | "aborted"
  | "contract_drift"
  | "duplicate_tool"
  | "invalid_definition"
  | "invalid_invocation"
  | "invalid_result"
  | "precondition_changed"
  | "unknown_tool";

export class ToolBrokerError extends Error {
  constructor(
    readonly code: ToolBrokerErrorCode,
    message: string,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ToolBrokerError";
  }
}
