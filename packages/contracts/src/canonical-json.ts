import type { JsonValue } from "./types.js";

const MAX_CANONICAL_DEPTH = 512;

export class CanonicalJsonError extends TypeError {
  readonly path: string;

  constructor(message: string, path = "$") {
    super(`${message} at ${path}`);
    this.name = "CanonicalJsonError";
    this.path = path;
  }
}

function assertValidUnicode(value: string, path: string): void {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!Number.isFinite(next) || next < 0xdc00 || next > 0xdfff) {
        throw new CanonicalJsonError("Unpaired high surrogate", path);
      }
      index += 1;
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      throw new CanonicalJsonError("Unpaired low surrogate", path);
    }
  }
}

function propertyPath(parent: string, key: string): string {
  return `${parent}[${JSON.stringify(key)}]`;
}

function serialize(
  value: unknown,
  path: string,
  ancestors: Set<object>,
  depth: number,
): string {
  if (depth > MAX_CANONICAL_DEPTH) {
    throw new CanonicalJsonError(
      `Nesting exceeds ${MAX_CANONICAL_DEPTH} levels`,
      path,
    );
  }

  if (value === null) return "null";

  switch (typeof value) {
    case "boolean":
      return value ? "true" : "false";
    case "number":
      if (!Number.isFinite(value)) {
        throw new CanonicalJsonError("Non-finite numbers are not JSON", path);
      }
      return JSON.stringify(value);
    case "string":
      assertValidUnicode(value, path);
      return JSON.stringify(value);
    case "undefined":
    case "bigint":
    case "function":
    case "symbol":
      throw new CanonicalJsonError(`Unsupported ${typeof value} value`, path);
    case "object":
      break;
    default:
      throw new CanonicalJsonError("Unsupported value", path);
  }

  if (ancestors.has(value)) {
    throw new CanonicalJsonError("Cyclic value", path);
  }
  ancestors.add(value);

  try {
    if (Array.isArray(value)) {
      const ownKeys = Reflect.ownKeys(value);
      for (const key of ownKeys) {
        if (key === "length") continue;
        if (
          typeof key !== "string" ||
          !/^(?:0|[1-9]\d*)$/.test(key) ||
          Number(key) >= value.length
        ) {
          throw new CanonicalJsonError("Array has a non-index property", path);
        }
      }

      const items: string[] = [];
      for (let index = 0; index < value.length; index += 1) {
        if (!Object.hasOwn(value, index)) {
          throw new CanonicalJsonError("Sparse arrays are not canonical JSON", path);
        }
        const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
        if (descriptor === undefined || !("value" in descriptor)) {
          throw new CanonicalJsonError(
            "Accessor properties are not canonical JSON",
            `${path}[${index}]`,
          );
        }
        items.push(
          serialize(descriptor.value, `${path}[${index}]`, ancestors, depth + 1),
        );
      }
      return `[${items.join(",")}]`;
    }

    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new CanonicalJsonError("Only plain objects can be canonicalized", path);
    }

    const descriptors = Object.getOwnPropertyDescriptors(value);
    const symbolKeys = Object.getOwnPropertySymbols(value);
    if (symbolKeys.length > 0) {
      throw new CanonicalJsonError("Symbol properties are not canonical JSON", path);
    }

    const keys = Object.keys(descriptors).sort();
    const members: string[] = [];
    for (const key of keys) {
      assertValidUnicode(key, propertyPath(path, key));
      const descriptor = descriptors[key];
      if (descriptor === undefined || !descriptor.enumerable) {
        throw new CanonicalJsonError(
          "Non-enumerable properties are not canonical JSON",
          propertyPath(path, key),
        );
      }
      if (!("value" in descriptor)) {
        throw new CanonicalJsonError(
          "Accessor properties are not canonical JSON",
          propertyPath(path, key),
        );
      }
      members.push(
        `${JSON.stringify(key)}:${serialize(
          descriptor.value,
          propertyPath(path, key),
          ancestors,
          depth + 1,
        )}`,
      );
    }
    return `{${members.join(",")}}`;
  } finally {
    ancestors.delete(value);
  }
}

/**
 * Deterministically encodes a strict JSON value.
 *
 * Object keys use UTF-16 lexical order and numbers use ECMAScript's shortest
 * round-trippable representation, matching the core of RFC 8785/JCS. Inputs
 * JSON itself cannot represent (including NaN and Infinity) are rejected.
 */
export function canonicalJson(value: unknown): string {
  return serialize(value, "$", new Set<object>(), 0);
}

export function canonicalJsonBytes(value: unknown): Uint8Array {
  return new TextEncoder().encode(canonicalJson(value));
}

export function assertJsonValue(value: unknown): asserts value is JsonValue {
  canonicalJson(value);
}
