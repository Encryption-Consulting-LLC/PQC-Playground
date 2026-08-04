import { Link, Outlet, useMatchRoute } from "@tanstack/react-router"
import { Badge } from "@/components/ui/badge"
import { LogoutButton } from "@/components/LogoutButton"
import { ThemeToggle } from "@/components/ThemeToggle"
import { useMe } from "@/hooks/useMe"
import { SECTIONS } from "@/nav"
import { cn } from "@/lib/utils"

/**
 * Hoisted so the active link gets a stable props object across renders. The
 * empty string still matches the `data-active:` variant, same as the
 * `data-active="true"` the old nav buttons rendered.
 */
const ACTIVE_PROPS = { "data-active": "" }

/**
 * Left-nav shell for the admin app — replaces the operator app's single
 * scrolling SettingsDialog with a persistent sidebar of sections, each its
 * own focused view instead of "one after another".
 *
 * This is the router's root component, so the nav items are real anchors: the
 * URL names the section, and middle-click, Back, and a pasted link all behave
 * the way they do everywhere else on the web.
 */
export function AppShell() {
  const me = useMe()
  const matchRoute = useMatchRoute()

  // Fuzzy, so "VM Registry" stays the title on /registry/orphans.
  const current = SECTIONS.find((section) => matchRoute({ to: section.to, fuzzy: true }))

  return (
    <div className="flex h-svh overflow-hidden">
      <aside className="flex w-56 shrink-0 flex-col gap-(--gap-stack) border-r bg-sidebar p-(--gap-row) text-sidebar-foreground">
        <div className="px-(--gap-inline) py-(--gap-inline)">
          <img
            src={`${import.meta.env.BASE_URL}ec-logo.png`}
            alt="PQC Playground"
            className="ec-logo mb-1 h-5"
          />
          <p className="text-xs text-muted-foreground">Admin</p>
        </div>
        <nav className="flex flex-1 flex-col gap-1">
          {SECTIONS.map(({ to, label, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              activeProps={ACTIVE_PROPS}
              // Not exact: /registry has to stay lit on /registry/orphans.
              activeOptions={{ exact: false, includeSearch: false }}
              className={cn(
                "flex items-center gap-(--gap-inline) rounded-lg px-(--pad-control) py-(--gap-inline) text-left text-sm font-medium text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                "data-active:bg-sidebar-accent data-active:text-sidebar-accent-foreground",
              )}
            >
              <Icon className="size-4 shrink-0" />
              {label}
            </Link>
          ))}
        </nav>
        <div className="border-t px-(--gap-inline) pt-(--gap-row) text-xs text-muted-foreground">
          Signed in as <Badge variant="outline">{me?.username}</Badge>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex shrink-0 items-center justify-between gap-(--gap-row) border-b px-(--pad-section) py-(--gap-row)">
          <h1 className="text-sm font-semibold tracking-tight">{current?.label ?? "Admin"}</h1>
          <div className="flex shrink-0 items-center gap-(--gap-inline)">
            <LogoutButton />
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1 overflow-y-auto p-(--pad-page)">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
