/**
 * The remote-desktop viewport: a Guacamole client bound to a DOM element.
 *
 * Shared by both SPAs — the admin app aliases this path rather than keeping a
 * second copy, because a protocol viewer is the wrong thing to maintain twice
 * (contrast `lib/ws.ts`, which is small enough that the two apps' copies have
 * deliberately diverged).
 *
 * This component owns the imperative Guacamole objects and nothing else. It is
 * handed a fully-resolved tunnel URL and the query data to dial it with, so its
 * only imports are React and the Guacamole library — no `@/` paths, which is
 * what lets one file serve two apps whose `@` alias points at different trees.
 * The caller owns the ticket lifecycle, because a ticket is single-use and a
 * reconnect is therefore a fresh authorization rather than a socket retry.
 *
 * Two details that are easy to get wrong and silent when wrong:
 *
 * - "Connected" is `Client.State.CONNECTED`, not tunnel-open. The tunnel opens
 *   as soon as guacd accepts the session, which it does *before* it has reached
 *   the guest at all — verified against guacd 1.5.0, which answers `ready` for a
 *   host that is not there and then simply waits. Reporting the tunnel as
 *   connected therefore shows a black screen for a powered-off VM with no
 *   explanation. The client reaches `CONNECTED` on its first `sync`, which means
 *   real frames.
 * - There is no RDP connect timeout in the Guacamole protocol (guacd 1.5.0
 *   exposes none among its 81 RDP parameters), so the deadline below is the only
 *   thing that turns an unreachable guest into a message instead of a hang.
 * - The keyboard is attached to `document`, because a desktop needs modifiers and
 *   arrow keys that never reach a focused div. It *must* be detached on unmount,
 *   or it keeps swallowing keystrokes — including the canvas's own Ctrl+Z undo
 *   handler — after the overlay is gone.
 * - `WebSocketTunnel.connect(data)` builds its socket as `` `${url}?${data}` ``,
 *   so the token rides in `tunnelData` and `tunnelUrl` must carry no query string
 *   of its own (each app's `lib/ws.ts` has the helpers that honour this).
 */

import Guacamole from "guacamole-common-js"
import { useEffect, useRef } from "react"

/** X11 keysyms for the one key combination a browser will never deliver. */
const CTRL_ALT_DEL = [0xffe3, 0xffe9, 0xffff] as const

/** `Guacamole.Client.State` members this component reacts to. */
const CLIENT_CONNECTED = 3
const CLIENT_DISCONNECTED = 5

/** How long the guest has to produce its first frame before we say so.
 * Generous: a VM still finishing its first boot legitimately takes a while to
 * answer on 3389. */
const CONNECT_DEADLINE_MS = 45_000

export interface GuacDisplayProps {
  /** Absolute `ws(s)://` URL, *without* a query string — the library appends one. */
  tunnelUrl: string
  /** Query string the library appends, e.g. `token=…`. */
  tunnelData: string
  /** Called once the tunnel is open and the first frame is on its way. */
  onConnected?: () => void
  /** Terminal failure, already phrased for a person. */
  onError?: (detail: string) => void
  /** The tunnel closed cleanly (the guest ended the session, or teardown). */
  onDisconnected?: (code: number | undefined) => void
  /** Imperative escape hatch for the Ctrl+Alt+Del button in the chrome. */
  actionsRef?: React.RefObject<{ sendCtrlAltDel: () => void } | null>
}

export function GuacDisplay({
  tunnelUrl,
  tunnelData,
  onConnected,
  onError,
  onDisconnected,
  actionsRef,
}: GuacDisplayProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  // Handlers are read through a ref so a parent re-render (a status change, say)
  // cannot retear and redial the tunnel — which, with single-use tickets, would
  // fail on the second dial and read as a flaky connection. Synced in an effect
  // rather than assigned during render, and declared above the tunnel effect so
  // it is already current the first time the tunnel reads it.
  const handlers = useRef({ onConnected, onError, onDisconnected })
  useEffect(() => {
    handlers.current = { onConnected, onError, onDisconnected }
  }, [onConnected, onError, onDisconnected])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const tunnel = new Guacamole.WebSocketTunnel(tunnelUrl)
    const client = new Guacamole.Client(tunnel)
    const display = client.getDisplay()
    const element = display.getElement()
    container.append(element)

    const mouse = new Guacamole.Mouse(element)
    mouse.onmousedown = (state) => client.sendMouseState(state)
    mouse.onmouseup = (state) => client.sendMouseState(state)
    mouse.onmousemove = (state) => client.sendMouseState(state)

    const keyboard = new Guacamole.Keyboard(document)
    keyboard.onkeydown = (keysym) => {
      client.sendKeyEvent(1, keysym)
      // Swallow the browser's own handling: otherwise Ctrl+W, Ctrl+T and friends
      // act on the tab instead of the guest.
      return false
    }
    keyboard.onkeyup = (keysym) => client.sendKeyEvent(0, keysym)

    if (actionsRef) {
      actionsRef.current = {
        sendCtrlAltDel: () => {
          for (const keysym of CTRL_ALT_DEL) client.sendKeyEvent(1, keysym)
          for (const keysym of [...CTRL_ALT_DEL].reverse())
            client.sendKeyEvent(0, keysym)
        },
      }
    }

    let settled = false
    const settle = (report: () => void) => {
      if (settled) return
      settled = true
      window.clearTimeout(deadline)
      report()
    }

    const deadline = window.setTimeout(
      () =>
        settle(() =>
          handlers.current.onError?.(
            "The guest did not respond. It may be powered off, still booting, or unreachable on the lab network.",
          ),
        ),
      CONNECT_DEADLINE_MS,
    )

    client.onerror = (status) =>
      settle(() =>
        handlers.current.onError?.(status.message || "The session ended."),
      )
    tunnel.onerror = (status) =>
      settle(() =>
        handlers.current.onError?.(status.message || "The connection failed."),
      )
    client.onstatechange = (state) => {
      if (state === CLIENT_CONNECTED) {
        // First `sync` — the guest is actually painting.
        window.clearTimeout(deadline)
        handlers.current.onConnected?.()
      }
      // A disconnect with no prior error is the ordinary ending: the user closed
      // the overlay, signed out inside the guest, or the VM was torn down.
      if (state === CLIENT_DISCONNECTED) {
        settle(() => handlers.current.onDisconnected?.(undefined))
      }
    }

    // Resize the guest to match the viewport. `resize-method=display-update` on
    // the backend's connection parameters is what makes this take effect without
    // reconnecting.
    const resize = () => {
      const { width, height } = container.getBoundingClientRect()
      if (width > 0 && height > 0) {
        client.sendSize(Math.floor(width), Math.floor(height))
      }
    }
    const observer = new ResizeObserver(resize)
    observer.observe(container)

    client.connect(tunnelData)

    return () => {
      observer.disconnect()
      // Order matters: release the document-level keyboard before anything else,
      // so a throw further down cannot leave it attached and eating keystrokes.
      keyboard.onkeydown = null
      keyboard.onkeyup = null
      keyboard.reset()
      mouse.onmousedown = null
      mouse.onmouseup = null
      mouse.onmousemove = null
      window.clearTimeout(deadline)
      client.onerror = null
      client.onstatechange = null
      tunnel.onerror = null
      if (actionsRef) actionsRef.current = null
      try {
        client.disconnect()
      } catch {
        // Already gone. Nothing to release that the GC will not take.
      }
      element.remove()
    }
    // `tunnelUrl` carries the ticket id, so it *is* the identity of a session:
    // a new URL means a new dial, and nothing else here should cause one.
  }, [tunnelUrl, tunnelData, actionsRef])

  return (
    <div
      ref={containerRef}
      className="flex h-full w-full items-center justify-center overflow-hidden bg-black"
      // Load-bearing, and invisible when missing. Guacamole gives every layer's
      // canvas `z-index: -1`, so each one paints behind the nearest ancestor that
      // establishes a stacking context — and with none here that is the *root*,
      // i.e. behind this container's own background and behind the overlay's
      // `z-50` popup. The session then works perfectly and shows a black
      // rectangle: frames arrive, the client reports CONNECTED, `toDataURL()` on
      // the canvas returns the full desktop, and nothing is composited. Isolating
      // makes this element the stacking context, so its background paints first
      // and the negative-z canvases land on top of it. Set inline rather than as
      // Tailwind's `isolate`, because this file is shared with `admin/` and sits
      // outside that app's Tailwind source root: the utility would only be
      // emitted there for as long as some *other* admin component happens to use
      // it. The one giveaway while it was broken: the mouse cursor was visible,
      // because Guacamole gives the cursor layer a `transform` — which is a
      // stacking context — and nothing else.
      style={{ isolation: "isolate" }}
      // The guest owns the keyboard while this is mounted; a focusable container
      // is what makes click-to-type work without a stray tab stop.
      tabIndex={-1}
    />
  )
}
