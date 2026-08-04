import { Link, Outlet, useMatchRoute } from "@tanstack/react-router"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsList, TabsPanel, TabsTab } from "@/components/ui/tabs"
import { TeardownProgressCard } from "./registry/TeardownProgressCard"

const TABS = [
  { value: "environments", label: "Environments", to: "/registry/environments" },
  { value: "orphans", label: "Orphans", to: "/registry/orphans" },
  { value: "all", label: "All VMs", to: "/registry/all" },
] as const

/**
 * VM registry oversight and teardown.
 *
 * Three views of the same collection: Environments groups it into the labs it
 * was deployed as and is where machines get reclaimed, Orphans surfaces the
 * entries that look abandoned, and All VMs stays the flat "just show me
 * everything" table. Each is a child route, so a tab is an address you can
 * hand to someone.
 *
 * This component is the layout for all three, which is what keeps the progress
 * card above the `Outlet`: a teardown started from Environments keeps running
 * while the admin reads Orphans, and rows for destroyed VMs vanish on the next
 * refetch. The tree shape now enforces that rather than a comment asking for
 * it.
 */
export function RegistrySection() {
  const matchRoute = useMatchRoute()
  const active = TABS.find((tab) => matchRoute({ to: tab.to }))?.value ?? TABS[0].value

  return (
    <div className="space-y-(--gap-stack)">
      <TeardownProgressCard />
      <Card>
        <CardHeader>
          <CardTitle>VM registry</CardTitle>
          <CardDescription>
            App-side identity and status cache for cloned machines, across every project.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Tabs value={active}>
            <TabsList>
              {TABS.map((tab) => (
                // Each tab is a real anchor, so middle-click and ⌘-click work.
                // Activation is therefore manual (Enter on a focused tab)
                // rather than on arrow-key focus — the correct ARIA pattern
                // when activating a tab means navigating.
                <TabsTab
                  key={tab.value}
                  value={tab.value}
                  nativeButton={false}
                  render={<Link to={tab.to} />}
                >
                  {tab.label}
                </TabsTab>
              ))}
            </TabsList>

            {/* One panel, always valued at the active tab, so Base UI still
                wires role="tabpanel" and aria-labelledby to a real tab. */}
            <TabsPanel value={active} className="mt-(--gap-stack)">
              <Outlet />
            </TabsPanel>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  )
}
