import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CanonicalJsonError,
  canonicalJson,
  canonicalJsonBytes,
} from "../src/index.js";

test("canonicalJson deterministically sorts nested object keys", () => {
  const value = {
    z: 1,
    a: { y: true, x: [null, -0, "snowman ☃"] },
  };

  assert.equal(
    canonicalJson(value),
    '{"a":{"x":[null,0,"snowman ☃"],"y":true},"z":1}',
  );
  assert.deepEqual(
    canonicalJsonBytes(value),
    new TextEncoder().encode(canonicalJson(value)),
  );
});

test("canonicalJson rejects every non-finite number", () => {
  for (const value of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
    assert.throws(
      () => canonicalJson({ nested: [value] }),
      (error: unknown) =>
        error instanceof CanonicalJsonError &&
        error.message.includes("Non-finite") &&
        error.path === '$["nested"][0]',
    );
  }
});

test("canonicalJson rejects ambiguous or non-JSON structures", () => {
  const sparse = new Array<unknown>(1);
  const cyclic: { self?: unknown } = {};
  cyclic.self = cyclic;

  assert.throws(() => canonicalJson({ missing: undefined }), CanonicalJsonError);
  assert.throws(() => canonicalJson(sparse), CanonicalJsonError);
  assert.throws(() => canonicalJson(cyclic), CanonicalJsonError);
  assert.throws(() => canonicalJson(new Date()), CanonicalJsonError);
  assert.throws(() => canonicalJson("\ud800"), CanonicalJsonError);
});
