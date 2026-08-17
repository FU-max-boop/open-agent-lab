export type EvidenceErrorCode =
  | "INVALID_INPUT"
  | "INVALID_PATH"
  | "DUPLICATE_PATH"
  | "LIMIT_EXCEEDED"
  | "TARGET_EXISTS"
  | "IO_ERROR"
  | "INVALID_MANIFEST"
  | "MANIFEST_ID_MISMATCH"
  | "SIZE_MISMATCH"
  | "HASH_MISMATCH"
  | "UNDECLARED_ENTRY"
  | "UNSAFE_ENTRY";

export class EvidenceError extends Error {
  readonly code: EvidenceErrorCode;

  constructor(code: EvidenceErrorCode, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "EvidenceError";
    this.code = code;
  }
}

export function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error;
}
