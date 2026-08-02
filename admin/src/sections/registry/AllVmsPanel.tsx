import { useQuery } from "@tanstack/react-query"
import { getRouteApi } from "@tanstack/react-router"
import { Loader2 } from "lucide-react"

import { QUERY_KEYS } from "@/constants"
import { listVmRegistry } from "@/lib/api"
import { formatDate, statusVariant } from "@/lib/display"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

// Fetched by id rather than importing the route object: routes.tsx imports
// this module, so importing it back would be a cycle.
const route = getRouteApi("/registry/all")

/**
 * Read-only view of every registry entry. Entries are keyed by the real ESXi
 * inventory name; app names repeat across projects, so vmName is the natural
 * stable identity to scan for.
 *
 * The filter is client-side over the rows already fetched — this is the flat
 * "just show me everything" table, and once it's loaded, narrowing it doesn't
 * need the server. It lives in the URL so a filtered view is shareable.
 */
export function AllVmsPanel() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.registry,
    queryFn: listVmRegistry,
    refetchInterval: 15_000,
  })

  const { q } = route.useSearch()
  const navigate = route.useNavigate()

  // `replace` so typing a filter doesn't bury the previous page under one
  // history entry per keystroke.
  const setQuery = (value: string) =>
    void navigate({ search: { q: value || undefined }, replace: true })

  const needle = (q ?? "").trim().toLowerCase()
  const entries = (data?.entries ?? []).filter((entry) =>
    !needle
      ? true
      : [entry.vmName, entry.appName, entry.projectId, entry.status, entry.ip]
          .some((field) => field?.toLowerCase().includes(needle)),
  )

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
    <div className="space-y-(--gap-row)">
      <Input
        aria-label="Filter VMs"
        placeholder="Filter by name, app, project, status, or IP…"
        value={q ?? ""}
        onChange={(e) => setQuery(e.target.value)}
        className="max-w-sm"
      />

      {entries.length === 0 ? (
        <p className="py-(--pad-section) text-xs text-muted-foreground">
          No VMs match “{q}”.
        </p>
      ) : (
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
            {entries.map((entry) => (
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
      )}
    </div>
  )
}
