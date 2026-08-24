import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Ban, Copy, KeyRound, Loader2 } from "lucide-react"
import { toast } from "sonner"

import { QUERY_KEYS } from "@/constants"
import { createLabInvite, listLabs, patchLabInvite, type Lab } from "@/lib/api"
import { formatDate, showError } from "@/lib/display"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

/**
 * Join codes for pre-deployed labs — the issuing half of the feature whose
 * redeeming half lives in the canvas app.
 *
 * A lab here is a deployed environment (the same `(owner, project)` grouping
 * the VM Registry's Environments tab shows) that also has a saved topology to
 * hand a joiner. Both halves are required: VMs with no project would resolve to
 * an empty canvas, which is why `snapshotAvailable` gates the mint button
 * rather than the request failing after the click.
 *
 * Revoke is destructive in the only sense that applies here — it cuts every
 * account that redeemed the code off from the lab and its desktops — so it is
 * styled as such and confirmed in place. The lab's VMs are untouched; this
 * section never destroys anything (that is the VM Registry's job), which is why
 * nothing on this page is red except the revoke control.
 */
export function LabsSection() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.labs,
    queryFn: listLabs,
    refetchInterval: 30_000,
  })
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null)

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.labs })

  const mintMutation = useMutation({
    mutationFn: (lab: Lab) =>
      createLabInvite({
        projectId: lab.projectId!,
        label: lab.projectName ?? undefined,
      }),
    onSuccess: (invite) => {
      invalidate()
      toast.success(`Join code ${invite.displayCode} created.`)
    },
    onError: showError,
  })

  const revokeMutation = useMutation({
    mutationFn: ({ code, revoked }: { code: string; revoked: boolean }) =>
      patchLabInvite(code, { revoked }),
    onSuccess: (invite) => {
      invalidate()
      setConfirmRevoke(null)
      toast.success(
        invite.revoked
          ? `Join code ${invite.displayCode} revoked.`
          : `Join code ${invite.displayCode} restored.`,
      )
    },
    onError: showError,
  })

  async function copyCode(code: string) {
    try {
      await navigator.clipboard.writeText(code)
      toast.success("Join code copied.")
    } catch {
      toast.error("Could not copy the join code.")
    }
  }

  const labs = data?.labs ?? []

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-(--gap-row)">
          <div>
            <CardTitle>Labs</CardTitle>
            <CardDescription>
              Hand a deployed lab to the people it was built for. Everyone who
              redeems the code sees that topology and can open its desktops —
              nothing else. Revoking the code ends that access for all of them.
            </CardDescription>
          </div>
          {data && <Badge variant="outline">{data.count} environments</Badge>}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Loading labs…
          </div>
        ) : isError ? (
          <p className="py-(--pad-section) text-xs text-destructive">
            {error instanceof Error ? error.message : "Could not load labs."}
          </p>
        ) : labs.length === 0 ? (
          <p className="py-(--pad-section) text-xs text-muted-foreground">
            Nothing is deployed yet. Deploy a lab from the canvas first, then
            issue its code here.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Lab</TableHead>
                <TableHead>Owner</TableHead>
                <TableHead>VMs</TableHead>
                <TableHead>Join code</TableHead>
                <TableHead>Joined</TableHead>
                <TableHead>Deployed</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {labs.map((lab) => (
                <TableRow key={lab.key}>
                  <TableCell className="text-xs font-medium">
                    {lab.projectName ?? lab.projectCode ?? "Unattributed VMs"}
                  </TableCell>
                  <TableCell className="text-xs">
                    {lab.owner ?? "—"}
                    {lab.owner && !lab.ownerResolved && (
                      <span className="ml-1 text-muted-foreground">
                        (from VM name)
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    {lab.vmCount}
                    <span className="ml-1 text-muted-foreground">
                      · {lab.ipCount} addressed
                    </span>
                  </TableCell>
                  <TableCell>
                    {lab.invite ? (
                      <button
                        type="button"
                        onClick={() => void copyCode(lab.invite!.displayCode)}
                        title="Click to copy"
                        className="flex items-center gap-(--gap-inline) rounded-sm font-mono text-xs outline-none hover:text-primary focus-visible:ring-3 focus-visible:ring-ring/50"
                      >
                        {lab.invite.displayCode}
                        <Copy className="size-3 text-muted-foreground" />
                      </button>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell
                    className="text-xs"
                    title={lab.invite?.members.join(", ") || undefined}
                  >
                    {lab.invite ? `${lab.invite.memberCount} joined` : "—"}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatDate(lab.createdAt)}
                  </TableCell>
                  <TableCell className="text-right">
                    {lab.invite ? (
                      confirmRevoke === lab.invite.code ? (
                        <div className="flex justify-end gap-(--gap-inline)">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setConfirmRevoke(null)}
                          >
                            Cancel
                          </Button>
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={revokeMutation.isPending}
                            onClick={() =>
                              revokeMutation.mutate({
                                code: lab.invite!.code,
                                revoked: true,
                              })
                            }
                          >
                            Revoke for {lab.invite.memberCount}
                          </Button>
                        </div>
                      ) : (
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => setConfirmRevoke(lab.invite!.code)}
                        >
                          <Ban className="mr-1 size-3.5" />
                          Revoke
                        </Button>
                      )
                    ) : (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={
                          !lab.projectId ||
                          !lab.snapshotAvailable ||
                          mintMutation.isPending
                        }
                        title={
                          lab.snapshotAvailable
                            ? undefined
                            : "These VMs have no saved topology, so there is nothing to hand a joiner."
                        }
                        onClick={() => mintMutation.mutate(lab)}
                      >
                        <KeyRound className="mr-1 size-3.5" />
                        Issue code
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {/* The listing only ever carries a lab's *live* code, so a revoked one
            simply leaves the row offering "Issue code" again. Reopening a
            cohort's access is a fresh code by design — the backend allows one
            live code per lab, and reusing a revoked one would silently readmit
            everybody who still had it. */}
      </CardContent>
    </Card>
  )
}
