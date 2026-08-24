/**
 * The remote-desktop viewport: a Guacamole client bound to a DOM element.
 *
 * Shared by both SPAs — the admin app aliases this path rather than keeping a
 * second copy, because a protocol viewer is the wrong thing to maintain twice
 * (contrast `lib/ws.ts`, which is small enough that the two apps' copies have
 * deliberately diverged).
 *
 * This component owns the imperative Guacamole objects and nothing else. It takes
 * a ticket id and a token, dials the tunnel, and reports state upward; the caller
 * owns the ticket lifecycle, because a ticket is single-use and a reconnect is
 * therefore a fresh authorization rather than a socket retry.
 *
 * Two details that are easy to get wrong and silent when wrong:
 *
 * - The keyboard is attached to `document`, because a desktop needs modifiers and
 *   arrow keys that never reach a focused div. It *must* be detached on unmount,
 *   or it keeps swallowing keystrokes — including the canvas's own Ctrl+Z undo
 *   handler — after the overlay is gone.
 * - `WebSocketTunnel.connect(data)` builds its socket as `` `${url}?${data}` ``,
 *   so the token rides in `data` and the URL must carry no query string of its
 *   own (see `lib/ws.ts::consoleTunnelUrl`).
 */

import Guacamole from "guacamole-common-js"
import { useEffect, useRef } from "react"

import { consoleTunnelData, consoleTunnelUrl } from "@/lib/ws"

/** X11 keysyms for the one key combination a browser will never deliver. */
const CTRL_ALT_DEL = [0xffe3, 0xffe9, 0xffff] as const

export interface GuacDisplayProps {
  ticketId: string
  token: string | null | undefined
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
  ticketId,
  token,
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

    const tunnel = new Guacamole.WebSocketTunnel(consoleTunnelUrl(ticketId))
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
    client.onerror = (status) => {
      settled = true
      handlers.current.onError?.(status.message || "The session ended.")
    }
    tunnel.onerror = (status) => {
      settled = true
      handlers.current.onError?.(status.message || "The connection failed.")
    }
    // Tunnel state 2 is OPEN — set on the first instruction, which the backend
    // guarantees is the tunnel UUID frame.
    tunnel.onstatechange = (state) => {
      if (state === 2) handlers.current.onConnected?.()
      // 3 is CLOSED. A close without a prior error is the ordinary ending: the
      // user closed the overlay, the guest logged out, or the VM was torn down.
      if (state === 3 && !settled) handlers.current.onDisconnected?.(undefined)
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

    client.connect(consoleTunnelData(token))

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
      client.onerror = null
      tunnel.onerror = null
      tunnel.onstatechange = null
      if (actionsRef) actionsRef.current = null
      try {
        client.disconnect()
      } catch {
        // Already gone. Nothing to release that the GC will not take.
      }
      element.remove()
    }
    // `ticketId` is the identity of a session: a new ticket means a new dial,
    // and nothing else here should cause one.
  }, [ticketId, token, actionsRef])

  return (
    <div
      ref={containerRef}
      className="flex h-full w-full items-center justify-center overflow-hidden bg-black"
      // The guest owns the keyboard while this is mounted; a focusable container
      // is what makes click-to-type work without a stray tab stop.
      tabIndex={-1}
    />
  )
}
