// Must stay the first import. This module reads the IdP's ?code&state off the
// URL and scrubs them at load time; `createRouter` below snapshots
// `window.location`, so the scrub has to have already happened or the router
// and the address bar start out disagreeing.
import "@/lib/oidcCallback"

import { createRouter } from "@tanstack/react-router"

import { queryClient } from "@/lib/queryClient"
import { routeTree } from "@/routes"

export const router = createRouter({
  routeTree,
  // Vite builds this app with `base: "/admin/"` and honours it in dev too, so
  // dev and prod agree without a second source of truth.
  basepath: import.meta.env.BASE_URL,
  context: { queryClient },
  defaultPreload: false,
})

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}
