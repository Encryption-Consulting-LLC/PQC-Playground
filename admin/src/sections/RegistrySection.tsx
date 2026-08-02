import { useQuery } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"

import { QUERY_KEYS } from "@/constants"
import { listVmRegistry } from "@/lib/api"
import { formatDate, statusVariant } from "@/lib/display"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsList, TabsPanel, TabsTab } from "@/components/ui/tabs"
import { EnvironmentsPanel } from "./registry/EnvironmentsPanel"
import { OrphansPanel } from "./registry/OrphansPanel"
import { TeardownProgressCard } from "./registry/TeardownProgressCard"

/**
 * VM registry oversight and teardown.
 *
 * Three views of the same collection: Environments groups it into the labs it
 * was deployed as and is where machines get reclaimed, Orphans surfaces the
 * entries that look abandoned, and All VMs stays the flat "just show me
 * everything" table.
 *
 * The progress card sits above the tabs rather than inside a panel: a teardown
 * started from Environments keeps running while the admin reads Orphans, and
 * rows for destroyed VMs vanish on the next refetch.
 */
export function RegistrySection() {
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
          <Tabs defaultValue="environments">
            <TabsList>
              <TabsTab value="environments">Environments</TabsTab>
              <TabsTab value="orphans">Orphans</TabsTab>
              <TabsTab value="all">All VMs</TabsTab>
            </TabsList>

            <TabsPanel value="environments" className="mt-(--gap-stack)">
              <EnvironmentsPanel />
            </TabsPanel>
            <TabsPanel value="orphans" className="mt-(--gap-stack)">
              <OrphansPanel />
            </TabsPanel>
            <TabsPanel value="all" className="mt-(--gap-stack)">
              <AllVmsPanel />
            </TabsPanel>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  )
}

/**
 * Read-only view of every registry entry. Entries are keyed by the real ESXi
 * inventory name; app names repeat across projects, so vmName is the natural
 * stable identity to scan for.
 */
function AllVmsPanel() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.registry,
    queryFn: listVmRegistry,
    refetchInterval: 15_000,
  })

  if (isLoading) {
    return (
      <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading registry…
      </div>
    )
  }
  if (isError) {
    return (
      <p className="py-(--pad-section) text-xs text-destructive">
        {error instanceof Error ? error.message : "Could not load the VM registry."}
      </p>
    )
  }
  if (!data || data.entries.length === 0) {
    return (
      <p className="py-(--pad-section) text-xs text-muted-foreground">
        No VMs have been registered yet.
      </p>
    )
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>VM name</TableHead>
          <TableHead>App</TableHead>
          <TableHead>Project</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Power</TableHead>
          <TableHead>IP</TableHead>
          <TableHead>Updated</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.entries.map((entry) => (
          <TableRow key={entry.vmName}>
            <TableCell className="font-mono text-xs">{entry.vmName}</TableCell>
            <TableCell className="text-xs">{entry.appName}</TableCell>
            <TableCell className="text-xs text-muted-foreground">
              {entry.projectId ?? "—"}
            </TableCell>
            <TableCell>
              <Badge variant={statusVariant(entry.status)}>{entry.status}</Badge>
            </TableCell>
            <TableCell className="text-xs text-muted-foreground">
              {entry.powerState ?? "—"}
            </TableCell>
            <TableCell className="font-mono text-xs">{entry.ip ?? "—"}</TableCell>
            <TableCell className="text-xs text-muted-foreground">
              {formatDate(entry.updatedAt)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
