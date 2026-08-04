/**
 * `defaultHostname` mirrors the backend's `hostname_for` / `linux_hostname_for`
 * (`core/firstboot.py`). It has to: the authored `10-hostname` script and the
 * sequence engine's `NodeContext.hostname` both feed the same guest, so a
 * divergence promotes a DC under a different name than DNS was told about.
 *
 * The mirror used to keep the *tail* of an over-long name; the backend drops the
 * `guest-`/user segments and then keeps the head, which is what preserves the
 * fixed-width project code.
 */

import { expect, test } from "vitest"

import { defaultHostname, hostnameScriptName } from "@/lib/hostname"

test("an operator's plain node name passes through", () => {
  expect(defaultHostname("DC01", "windows")).toBe("DC01")
})

test("a guest namespace drops the guest literal and the user segment", () => {
  // The backend's own docstring example.
  expect(defaultHostname("guest-alice-467893-dc01", "windows")).toBe(
    "467893-dc01",
  )
})

test("an over-long windows name keeps the head, not the tail", () => {
  // 15-char NetBIOS limit. Head-truncation is what keeps the project code.
  expect(defaultHostname("467893-verylongmachinename", "windows")).toBe(
    "467893-verylong",
  )
})

test("unsafe characters become dashes and edges are trimmed", () => {
  expect(defaultHostname("web_server.01!", "windows")).toBe("web-server-01")
  expect(defaultHostname("---", "windows")).toBe("vm")
  expect(defaultHostname("", "windows")).toBe("vm")
})

test("a short guest name with no project segment is left alone", () => {
  // Only two segments — nothing to drop, so it is not a namespaced name.
  expect(defaultHostname("guest-dc01", "windows")).toBe("guest-dc01")
})

test("linux keeps the full namespace, lowercased, to 63 chars", () => {
  expect(defaultHostname("guest-alice-467893-DC01", "linux")).toBe(
    "guest-alice-467893-dc01",
  )
})

test("script name follows the platform's extension", () => {
  expect(hostnameScriptName("windows")).toBe("10-hostname.ps1")
  expect(hostnameScriptName("linux")).toBe("10-hostname.sh")
})
