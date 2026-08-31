/**
 * The product catalogue gate: what a grant covers, and what it deliberately
 * does not reach.
 */

import { expect, test } from "vitest"

import { isTemplateAvailable } from "@/lib/products"

test("a product is available only when the account was granted it", () => {
  expect(isTemplateAvailable("certsecure", ["certsecure"])).toBe(true)
  expect(isTemplateAvailable("codesign", ["certsecure"])).toBe(false)
  expect(isTemplateAvailable("certsecure", [])).toBe(false)
})

test("the gate fails closed while the identity is still loading", () => {
  // Greyed out until proven otherwise — a card that flashes as draggable and
  // then goes inert is worse than one that arrives already disabled.
  expect(isTemplateAvailable("certsecure", undefined)).toBe(false)
})

test("the PKI components are outside the catalogue", () => {
  // The grant restricts products, never the playground. An operator's resolved
  // list covers every product, so the same call answers for both roles.
  for (const typeId of [
    "domainController",
    "certificateAuthority",
    "webServer",
    "standalone",
  ]) {
    expect(isTemplateAvailable(typeId, [])).toBe(true)
  }
  expect(isTemplateAvailable("unknown-template", [])).toBe(true)
})
