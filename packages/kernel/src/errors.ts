export type KernelErrorCode =
  | "corrupt_journal"
  | "invalid_state"
  | "invalid_store"
  | "invocation_conflict"
  | "lease_held"
  | "lease_lost"
  | "run_id_mismatch"
  | "stale_head"
  | "target_exists"
  | "verifier_mismatch";

export class KernelError extends Error {
  constructor(
    readonly code: KernelErrorCode,
    message: string,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "KernelError";
  }
}
