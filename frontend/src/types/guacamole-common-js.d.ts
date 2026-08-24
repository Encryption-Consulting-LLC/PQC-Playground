/**
 * Minimal ambient types for `guacamole-common-js`, which ships none.
 *
 * Both apps run `tsc -b` as part of `build`, so an untyped import fails the
 * build outright. Rather than an `any` shim, this declares exactly the surface
 * the console viewer uses — so a typo in a method name is still a compile error,
 * and anything else the library offers has to be declared before it can be
 * reached for.
 *
 * Upstream reference: `guacamole-common-js/dist/esm/guacamole-common.js`.
 */
declare module "guacamole-common-js" {
  /** Mouse button/position snapshot handed to `sendMouseState`. */
  export interface MouseState {
    x: number
    y: number
    left: boolean
    middle: boolean
    right: boolean
    up: boolean
    down: boolean
  }

  export class Display {
    getElement(): HTMLElement
    getWidth(): number
    getHeight(): number
    scale(scale: number): void
    onresize: ((width: number, height: number) => void) | null
    /** Cursor hotspot, for `Mouse.setCursor`-style local cursor rendering. */
    getCursorLayer(): unknown
  }

  export class Tunnel {
    /** Reserved opcode for tunnel-level messages (the empty string). */
    static INTERNAL_DATA_OPCODE: string
    uuid: string | null
  }

  export class WebSocketTunnel extends Tunnel {
    constructor(tunnelURL: string)
    /** `data` is appended as the socket URL's query string by the library. */
    connect(data?: string): void
    disconnect(): void
    onerror: ((status: { code: number; message: string }) => void) | null
    onstatechange: ((state: number) => void) | null
  }

  export class Client {
    constructor(tunnel: Tunnel)
    getDisplay(): Display
    connect(data?: string): void
    disconnect(): void
    sendMouseState(state: MouseState): void
    sendKeyEvent(pressed: 0 | 1, keysym: number): void
    sendSize(width: number, height: number): void
    onerror: ((status: { code: number; message: string }) => void) | null
    onstatechange: ((state: number) => void) | null
    onname: ((name: string) => void) | null
  }

  export class Mouse {
    constructor(element: HTMLElement)
    onmousedown: ((state: MouseState) => void) | null
    onmouseup: ((state: MouseState) => void) | null
    onmousemove: ((state: MouseState) => void) | null
    onmouseout: (() => void) | null
  }

  export class Keyboard {
    constructor(element: HTMLElement | Document)
    onkeydown: ((keysym: number) => boolean | void) | null
    onkeyup: ((keysym: number) => void) | null
    reset(): void
  }

  const Guacamole: {
    Client: typeof Client
    Display: typeof Display
    Keyboard: typeof Keyboard
    Mouse: typeof Mouse
    Tunnel: typeof Tunnel
    WebSocketTunnel: typeof WebSocketTunnel
  }
  export default Guacamole
}
