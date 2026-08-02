import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest"

import { formatApiErrorDetail } from "@/lib/api"

const storage = new Map<string, string>()

vi.stubGlobal("localStorage", {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => storage.set(key, value),
  removeItem: (key: string) => storage.delete(key),
})

let getHealth: typeof import("@/lib/api")["getHealth"]
let useAuthStore: typeof import("@/store/auth")["useAuthStore"]

/** A 401, optionally marked by the backend as "the token you sent is dead". */
function unauthorized(sessionRejected: boolean) {
  const headers = new Headers()
  if (sessionRejected) headers.set("www-authenticate", "Session")
  return new Response(JSON.stringify({ detail: "nope" }), {
    status: 401,
    headers,
  })
}

beforeAll(async () => {
  ;({ getHealth } = await import("@/lib/api"))
  ;({ useAuthStore } = await import("@/store/auth"))
})

beforeEach(() => {
  storage.clear()
  useAuthStore.setState({
    token: "live-token",
    username: "operator",
    role: "operator",
  })
})

describe("formatApiErrorDetail", () => {
  it("surfaces failed preflight checks instead of stringifying the response", () => {
    expect(
      formatApiErrorDetail({
        message: "Control-plane preflight failed.",
        preflight: {
          ready: false,
          checks: [
            { key: "mongo", ok: true, detail: "MongoDB responded to ping." },
            {
              key: "agentBinary",
              ok: false,
              detail: "Agent digest does not match.",
            },
          ],
        },
      }),
    ).toBe("Control-plane preflight failed. Agent digest does not match.")
  })

  it("preserves ordinary FastAPI string details", () => {
    expect(formatApiErrorDetail("VM already exists.")).toBe(
      "VM already exists.",
    )
  })
})

describe("session auto-logout", () => {
  it("clears the session when the backend rejects the token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => unauthorized(true)),
    )

    await expect(getHealth()).rejects.toThrow()
    expect(useAuthStore.getState().token).toBeUndefined()
  })

  it("keeps the session on a 401 that is not about the token", async () => {
    // e.g. a downstream ESXi login failure on the deploy preflight — clearing
    // on that logged the operator out mid-deploy.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => unauthorized(false)),
    )

    await expect(getHealth()).rejects.toThrow()
    expect(useAuthStore.getState().token).toBe("live-token")
  })
})
