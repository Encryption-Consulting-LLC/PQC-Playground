import { useQuery } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"

import { QUERY_KEYS } from "@/constants"
import { getIpPool } from "@/lib/api"
import { formatDate } from "@/lib/display"
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

/**
 * Read-only view of the guest IP pool (core/ippool.py) — which addresses in
 * the configured range are allocated, to which VM, and how many remain free.
 * The range itself is edited in the Infrastructure section's guest network
 * tab; this is runtime allocation state, not configuration.
 */
export function IpPoolSection() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.ipPool,
    queryFn: getIpPool,
    refetchInterval: 15_000,
  })

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-(--gap-row)">
          <div>
            <CardTitle>IP pool</CardTitle>
            <CardDescription>
              One document per address in the configured guest range.
            </CardDescription>
          </div>
          {data && (
            <div className="flex gap-(--gap-inline)">
              <Badge variant="success">{data.free} free</Badge>
              <Badge variant="outline">{data.allocated} allocated</Badge>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Loading pool…
          </div>
        ) : isError ? (
          <p className="py-(--pad-section) text-xs text-destructive">
            {error instanceof Error ? error.message : "Could not load the IP pool."}
          </p>
        ) : data && data.entries.length === 0 ? (
          <p className="py-(--pad-section) text-xs text-muted-foreground">
            No guest network is configured yet — set one in Infrastructure → Guest network.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Address</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>VM</TableHead>
                <TableHead>Allocated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.entries.map((entry) => (
                <TableRow key={entry.ip}>
                  <TableCell className="font-mono text-xs">{entry.ip}</TableCell>
                  <TableCell>
                    <Badge variant={entry.status === "allocated" ? "outline" : "success"}>
                      {entry.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs">{entry.vmName ?? "—"}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatDate(entry.allocatedAt)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
