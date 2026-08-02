import { useQuery } from "@tanstack/react-query"
import { AlertTriangle, Loader2, Trash2, Wand2 } from "lucide-react"

import { QUERY_KEYS } from "@/constants"
import { previewTeardown } from "@/lib/api"
import { plural } from "@/lib/display"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBackdrop,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogPopup,
  DialogPortal,
  DialogTitle,
} from "@/components/ui/dialog"

const GRACEFUL_TOOLTIP =
  "Graceful teardown unjoins each VM from the domain, revokes its issued certificates and removes its CA role before destroying it. Not available yet — force destroy is the only mode today."

/**
 * Force-destroy confirmation, gated on a server-computed dry run.
 *
 * The preview is fetched fresh every time the dialog opens and the confirm
 * button stays disabled until it resolves: a safety rail that shows stale
 * numbers is worse than none, and you cannot confirm what you have not been
 * shown. The preview endpoint refuses (503) rather than guessing when the ESXi
 * host is unreachable, so "we could not tell" surfaces here as an error the
 * admin has to close, not as a confident-looking list.
 */
export function TeardownConfirmDialog({
  open,
  label,
  vmNames,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  label: string
  vmNames: string[]
  busy: boolean
  onCancel: () => void
  onConfirm: (confirmToken: string) => void
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: [...QUERY_KEYS.teardownPreview, ...vmNames],
    queryFn: () => previewTeardown(vmNames),
    enabled: open && vmNames.length > 0,
    // The rail is worthless if it is stale — never serve this from cache.
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always",
    retry: false,
  })

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogPortal>
        <DialogBackdrop />
        <DialogPopup className="w-[min(560px,calc(100vw-2rem))]">
          <DialogTitle className="flex items-center gap-(--gap-inline)">
            <Trash2 className="size-4 text-destructive" />
            Force destroy {label}?
          </DialogTitle>
          <DialogDescription>
            Every machine listed below is destroyed and its IP address returned to the
            pool. This cannot be undone.
          </DialogDescription>

          <DialogBody>
            {isLoading ? (
              <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" /> Computing what this removes…
              </div>
            ) : isError ? (
              <p className="py-(--pad-section) text-xs text-destructive">
                {error instanceof Error ? error.message : "Could not compute the preview."}{" "}
                Nothing has been destroyed — close this and try again.
              </p>
            ) : data ? (
              <>
                <div className="flex flex-wrap items-center gap-(--gap-inline)">
                  <Badge variant={data.toDestroy > 0 ? "destructive" : "outline"}>
                    {plural(data.toDestroy, "VM")} destroyed on ESXi
                  </Badge>
                  {data.alreadyAbsent > 0 && (
                    <Badge variant="outline">{data.alreadyAbsent} already absent</Badge>
                  )}
                  <Badge variant="info">{plural(data.releasesIp, "IP")} released</Badge>
                </div>

                {data.toDestroy === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    Nothing left to destroy — every VM here is already gone from the host.
                    Confirming just releases any held IP addresses and marks the registry
                    entries deleted.
                  </p>
                ) : null}

                {/* Its own scroller so the title and the warning never scroll away. */}
                <ul className="max-h-56 space-y-(--gap-inline) overflow-y-auto rounded-lg border p-(--pad-control)">
                  {data.vms.map((vm) => (
                    <li
                      key={vm.vmName}
                      className="flex flex-wrap items-center gap-(--gap-inline)"
                    >
                      <span className="font-mono text-xs">{vm.vmName}</span>
                      <span className="font-mono text-xs text-muted-foreground">
                        {vm.releasesIp ?? vm.ip ?? "—"}
                      </span>
                      <Badge variant="outline">{vm.status}</Badge>
                      {vm.presentInEsxi === false && (
                        <Badge variant="warning">already absent</Badge>
                      )}
                    </li>
                  ))}
                </ul>

                <div className="flex gap-(--gap-inline) rounded-lg bg-warning/10 p-(--pad-control) text-xs text-warning">
                  <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                  <span>
                    Force destroy runs nothing inside the guests. Domain accounts, DNS
                    records, issued certificates and CA state are left behind in Active
                    Directory.
                  </span>
                </div>
              </>
            ) : null}
          </DialogBody>

          <DialogFooter className="justify-between">
            {/* Left-aligned, opposite the real actions: this is the moment the
                admin is choosing *how* to tear down, so an unavailable peer
                control reads as "there are two modes, one isn't ready". The
                wrapper carries the tooltip because a disabled Button has
                `pointer-events-none` and its own title would never fire. */}
            <span title={GRACEFUL_TOOLTIP}>
              <Button variant="outline" size="sm" disabled>
                <Wand2 className="mr-1 size-3.5" />
                Graceful teardown
                <Badge variant="info" className="ml-1">
                  soon
                </Badge>
              </Button>
            </span>
            <div className="flex gap-(--gap-inline)">
              <Button variant="outline" size="sm" onClick={onCancel}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={!data || busy}
                onClick={() => data && onConfirm(data.confirmToken)}
              >
                <Trash2 className="mr-1 size-3.5" />
                Force destroy
              </Button>
            </div>
          </DialogFooter>
        </DialogPopup>
      </DialogPortal>
    </Dialog>
  )
}
