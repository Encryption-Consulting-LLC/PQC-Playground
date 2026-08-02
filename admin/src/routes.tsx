import type { QueryClient } from "@tanstack/react-query"
import { createRootRouteWithContext, createRoute, redirect } from "@tanstack/react-router"
import { FileQuestion } from "lucide-react"

import { AppShell } from "@/components/AppShell"
import { UsersSection } from "@/sections/UsersSection"
import { InfrastructureSection } from "@/sections/InfrastructureSection"
import { DeploymentsSection } from "@/sections/DeploymentsSection"
import { IpPoolSection } from "@/sections/IpPoolSection"
import { RegistrySection } from "@/sections/RegistrySection"
import { EnvironmentsPanel } from "@/sections/registry/EnvironmentsPanel"
import { OrphansPanel } from "@/sections/registry/OrphansPanel"
import { AllVmsPanel } from "@/sections/registry/AllVmsPanel"

export interface RouterContext {
  /**
   * Carried so route `loader`s can prefetch later without restructuring the
   * router. Nothing uses it yet — the sections still fetch in components.
   */
  queryClient: QueryClient
}

/**
 * An address that isn't a section. Rendered in place of the root `Outlet`, so
 * it keeps the sidebar: a mistyped URL should leave you somewhere you can
 * navigate out of, not on a bare error page.
 */
function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center gap-(--gap-row) py-(--pad-page) text-center">
      <FileQuestion className="size-8 text-muted-foreground" />
      <p className="text-sm font-medium">No such page.</p>
      <p className="max-w-sm text-xs text-muted-foreground">
        That address doesn&apos;t match a section of the admin console. Pick one from the
        sidebar.
      </p>
    </div>
  )
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: AppShell,
  notFoundComponent: NotFound,
})

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  // `replace` so landing on /admin and bouncing to /accounts doesn't leave a
  // history entry that Back immediately bounces off again.
  beforeLoad: () => {
    throw redirect({ to: "/accounts", replace: true })
  },
})

const accountsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/accounts",
  component: UsersSection,
})

const infrastructureRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/infrastructure",
  component: InfrastructureSection,
})

const deploymentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/deployments",
  component: DeploymentsSection,
})

const ipPoolRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/ip-pool",
  component: IpPoolSection,
})

// A layout, not a leaf: RegistrySection renders the teardown progress card
// above the tab Outlet, so a running teardown survives a tab change.
const registryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/registry",
  component: RegistrySection,
})

const registryIndexRoute = createRoute({
  getParentRoute: () => registryRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/registry/environments", replace: true })
  },
})

const registryEnvironmentsRoute = createRoute({
  getParentRoute: () => registryRoute,
  path: "/environments",
  component: EnvironmentsPanel,
})

const registryOrphansRoute = createRoute({
  getParentRoute: () => registryRoute,
  path: "/orphans",
  component: OrphansPanel,
})

const registryAllRoute = createRoute({
  getParentRoute: () => registryRoute,
  path: "/all",
  component: AllVmsPanel,
})

export const routeTree = rootRoute.addChildren([
  indexRoute,
  accountsRoute,
  infrastructureRoute,
  deploymentsRoute,
  ipPoolRoute,
  registryRoute.addChildren([
    registryIndexRoute,
    registryEnvironmentsRoute,
    registryOrphansRoute,
    registryAllRoute,
  ]),
])
