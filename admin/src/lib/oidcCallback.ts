/**
 * The IdP's returning `?code&state`, captured and scrubbed at module load.
 *
 * This runs before `createRouter`, and that ordering is the point. The router
 * takes `window.location` as its initial state, so scrubbing the params later
 * — as the old `history.replaceState` in `LoginForm` did — would leave the
 * router's idea of the location disagreeing with the address bar, and a
 * subsequent navigation could resurrect a consumed code.
 *
 * Module scope also makes the exchange exactly-once for real. A `useRef` guard
 * only approximated that: StrictMode double-invokes effects, and the params
 * stayed readable from `window.location` until the effect got round to them.
 *
 * Only `code` and `state` are removed. Everything else in the query string
 * (`?expanded=`, `?q=`) and the hash survive, so an SSO round-trip doesn't
 * quietly discard where the admin was headed.
 */
interface OidcCallback {
  code: string
  state: string
}

function capture(): OidcCallback | null {
  const url = new URL(window.location.href)
  const code = url.searchParams.get("code")
  const state = url.searchParams.get("state")
  if (!code || !state) return null

  url.searchParams.delete("code")
  url.searchParams.delete("state")
  window.history.replaceState(null, "", url.pathname + url.search + url.hash)
  return { code, state }
}

export const OIDC_CALLBACK = capture()
