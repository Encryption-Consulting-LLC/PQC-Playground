import { useQuery } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"

import { QUERY_KEYS } from "@/constants"
import { listVmRegistry } from "@/lib/api"
import { formatDate, statusVariant } from "@/lib/display"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

/**
 * Read-only view of every registry entry. Entries are keyed by the real ESXi
 * inventory name; app names repeat across projects, so vmName is the natural
 * stable identity to scan for.
 */
export function AllVmsPanel() {
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
