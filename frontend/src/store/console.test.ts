/**
 * The console store's session lifecycle.
 *
 * Two behaviours are worth pinning. Reconnect has to mint a *fresh* ticket —
 * tickets are single-use, so redialling the old one always fails, and an earlier
 * version of this store left Reconnect setting a status nothing acted on, which
 * spun the overlay forever.
 *
 * And every async settle is guarded on still being the session it started for.
 * The authorization round trip is a network call the user can outrun by closing
 * the overlay or opening another node, and an unguarded `set` there resurrects a
 * closed session or hijacks a visible one with another VM's ticket.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "@/lib/api"
import { useConsoleStore } from "@/store/console"

const mintConsoleTicket = vi.hoisted(() => vi.fn())

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  mintConsoleTicket,
}))

const NODE = { nodeId: "n1", vmName: "guest-alice-9e4edb-ca01", label: "CA01" }

function ticket(ticketId: string) {
  return {
    ticketId,
    vmName: NODE.vmName,
    credentialLabel: "Administrator (local)",
    expiresInSeconds: 60,
  }
}

beforeEach(() => {
  mintConsoleTicket.mockReset()
  useConsoleStore.setState({ session: null })
})

describe("start", () => {
  it("authorizes and moves to connecting with a ticket", async () => {
    mintConsoleTicket.mockResolvedValue(ticket("tkt-1"))

    await useConsoleStore.getState().start(NODE)

    const session = useConsoleStore.getState().session
    expect(session?.status).toBe("connecting")
    expect(session?.ticketId).toBe("tkt-1")
    expect(session?.credentialLabel).toBe("Administrator (local)")
    expect(mintConsoleTicket).toHaveBeenCalledWith(NODE.vmName)
  })

  it("surfaces the backend's own wording on a refusal", async () => {
    // The 409 detail is the actionable half — "redeploy this node" is a fix, and
    // a generic "could not connect" would throw it away.
    mintConsoleTicket.mockRejectedValue(
      new ApiError(409, "redeploy this node to enable it"),
    )

    await useConsoleStore.getState().start(NODE)

    const session = useConsoleStore.getState().session
    expect(session?.status).toBe("error")
    expect(session?.detail).toBe("redeploy this node to enable it")
    expect(session?.ticketId).toBeUndefined()
  })

  it("falls back to a generic message on a non-API failure", async () => {
    mintConsoleTicket.mockRejectedValue(new TypeError("network down"))

    await useConsoleStore.getState().start(NODE)

    expect(useConsoleStore.getState().session?.detail).toContain(
      "Could not start",
    )
  })
})

describe("reconnect", () => {
  it("mints a new ticket rather than redialling the spent one", async () => {
    mintConsoleTicket.mockResolvedValueOnce(ticket("tkt-1"))
    await useConsoleStore.getState().start(NODE)

    mintConsoleTicket.mockResolvedValueOnce(ticket("tkt-2"))
    await useConsoleStore.getState().reconnect()

    const session = useConsoleStore.getState().session
    expect(session?.ticketId).toBe("tkt-2")
    expect(session?.attempt).toBe(1)
    expect(mintConsoleTicket).toHaveBeenCalledTimes(2)
  })

  it("clears the stale detail so the error screen does not linger", async () => {
    mintConsoleTicket.mockRejectedValueOnce(new ApiError(503, "guacd is down"))
    await useConsoleStore.getState().start(NODE)
    expect(useConsoleStore.getState().session?.status).toBe("error")

    mintConsoleTicket.mockResolvedValueOnce(ticket("tkt-2"))
    await useConsoleStore.getState().reconnect()

    const session = useConsoleStore.getState().session
    expect(session?.status).toBe("connecting")
    expect(session?.detail).toBeUndefined()
  })

  it("is a no-op with no session", async () => {
    await useConsoleStore.getState().reconnect()

    expect(useConsoleStore.getState().session).toBeNull()
    expect(mintConsoleTicket).not.toHaveBeenCalled()
  })
})

describe("staleness guards", () => {
  it("does not resurrect a session the user closed mid-authorization", async () => {
    let resolve: (value: unknown) => void = () => {}
    mintConsoleTicket.mockReturnValue(
      new Promise((r) => {
        resolve = r
      }),
    )
    const pending = useConsoleStore.getState().start(NODE)

    useConsoleStore.getState().close()
    resolve(ticket("tkt-1"))
    await pending

    expect(useConsoleStore.getState().session).toBeNull()
  })

  it("does not let a superseded attempt overwrite the live one", async () => {
    // Reconnect twice in flight: the first ticket must not land on top of the
    // second, or the viewer dials a ticket the user already abandoned.
    mintConsoleTicket.mockResolvedValueOnce(ticket("tkt-1"))
    await useConsoleStore.getState().start(NODE)

    let resolveSlow: (value: unknown) => void = () => {}
    mintConsoleTicket.mockReturnValueOnce(
      new Promise((r) => {
        resolveSlow = r
      }),
    )
    const slow = useConsoleStore.getState().reconnect()

    mintConsoleTicket.mockResolvedValueOnce(ticket("tkt-3"))
    await useConsoleStore.getState().reconnect()

    resolveSlow(ticket("tkt-2"))
    await slow

    expect(useConsoleStore.getState().session?.ticketId).toBe("tkt-3")
  })

  it("does not carry one node's ticket onto another node's session", async () => {
    let resolve: (value: unknown) => void = () => {}
    mintConsoleTicket.mockReturnValueOnce(
      new Promise((r) => {
        resolve = r
      }),
    )
    const pending = useConsoleStore.getState().start(NODE)

    mintConsoleTicket.mockResolvedValueOnce(ticket("tkt-dc"))
    await useConsoleStore
      .getState()
      .start({ nodeId: "n2", vmName: "guest-alice-9e4edb-dc01", label: "DC01" })

    resolve(ticket("tkt-ca"))
    await pending

    const session = useConsoleStore.getState().session
    expect(session?.nodeId).toBe("n2")
    expect(session?.ticketId).toBe("tkt-dc")
  })
})
