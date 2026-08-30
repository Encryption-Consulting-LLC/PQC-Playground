/**
 * The job socket's silence watchdog.
 *
 * `onclose` used to be the only thing this client reacted to, which is fine
 * right up until the socket dies without one. A deploy plan is silent for whole
 * role installs (`dc.install_forest` and `ca.install` each run 10+ minutes
 * emitting nothing), anything in the path reaps an idle WebSocket — nginx at
 * 60s, Cloudflare around 100s — and that drop does not reliably reach the
 * browser as a close event. The client then holds a half-open socket with no
 * reason to reconnect, so the terminal frame is published into a connection
 * nobody is reading: one real run sat "deploying" on the canvas for 8h31m after
 * the plan had already succeeded, until the page was reloaded.
 *
 * So silence itself has to be a failure the caller hears about, and the
 * backend's heartbeat is what makes ordinary quiet distinguishable from death.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { openJobSocket } from "@/lib/ws"

class FakeWebSocket {
  static last: FakeWebSocket | null = null

  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  closed = false
  closeCalls = 0

  url: string

  constructor(url: string) {
    this.url = url
    FakeWebSocket.last = this
  }

  close() {
    this.closed = true
    this.closeCalls += 1
  }

  /** Deliver one server frame. */
  emit(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) })
  }
}

const SILENCE_MS = 60_000

beforeEach(() => {
  vi.useFakeTimers()
  FakeWebSocket.last = null
  vi.stubGlobal("WebSocket", FakeWebSocket)
  vi.stubGlobal("window", {
    location: { protocol: "http:", host: "localhost:5432" },
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function attach() {
  const onError = vi.fn()
  const onDone = vi.fn()
  const onPlanState = vi.fn()
  const close = openJobSocket("job9", "tok", { onError, onDone, onPlanState })
  // biome-ignore lint/style/noNonNullAssertion: the constructor just ran
  return { socket: FakeWebSocket.last!, onError, onDone, onPlanState, close }
}

describe("openJobSocket", () => {
  it("reports a silent socket as a recoverable transport failure", () => {
    const { socket, onError } = attach()

    vi.advanceTimersByTime(SILENCE_MS)

    expect(onError).toHaveBeenCalledTimes(1)
    // `status: 0` plus a code that is neither 4401 nor 4404 is precisely what
    // the caller's retry ladder treats as retryable — and the reconnect is what
    // finally delivers the terminal frame.
    expect(onError.mock.calls[0][0]).toMatchObject({ status: 0, code: 1006 })
    expect(socket.closed).toBe(true)
  })

  it("stays quiet while the backend is heartbeating", () => {
    const { socket, onError, onPlanState } = attach()

    // Three quiet-but-alive intervals: the heartbeat is the only traffic.
    for (let i = 0; i < 3; i++) {
      vi.advanceTimersByTime(SILENCE_MS - 1_000)
      socket.emit({ type: "ping" })
    }

    expect(onError).not.toHaveBeenCalled()
    // A heartbeat is transport bookkeeping, not a job event.
    expect(onPlanState).not.toHaveBeenCalled()

    // ...and once the heartbeats stop, the watchdog still fires.
    vi.advanceTimersByTime(SILENCE_MS)
    expect(onError).toHaveBeenCalledTimes(1)
  })

  it("does not fire after the job has already finished", () => {
    const { socket, onDone, onError } = attach()

    socket.emit({ type: "done", result: {} })
    vi.advanceTimersByTime(SILENCE_MS * 3)

    expect(onDone).toHaveBeenCalledTimes(1)
    // The stream is meant to go quiet after a terminal frame — treating that as
    // a dead socket would reopen and re-report a finished deploy.
    expect(onError).not.toHaveBeenCalled()
  })

  it("reports a real close once, not twice", () => {
    const { socket, onError } = attach()

    socket.onclose?.({ code: 1006 })
    vi.advanceTimersByTime(SILENCE_MS * 2)

    expect(onError).toHaveBeenCalledTimes(1)
  })

  it("stops the watchdog when the caller detaches", () => {
    const { onError, close } = attach()

    close()
    vi.advanceTimersByTime(SILENCE_MS * 2)

    expect(onError).not.toHaveBeenCalled()
  })
})
