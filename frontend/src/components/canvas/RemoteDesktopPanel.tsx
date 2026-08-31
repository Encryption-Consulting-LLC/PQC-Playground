/**
 * The Inspector's Remote Desktop section: one button, and an honest reason when
 * there is no button to press.
 *
 * Pulls its own state from the topology store (the `IsoAuthoringPanel` pattern)
 * so the Inspector only has to render `<RemoteDesktopPanel nodeId />`.
 *
 * The panel does the authorization round trip itself rather than letting the
 * overlay do it, because that is where every actionable refusal comes from and
 * the Inspector is where the user can act on it. A 409 means "redeploy this
 * node" or "wait for the clone"; a 503 means "the service is down, not your
 * lab". Surfacing those here — next to the node, before anything full-screen
 * opens — is the difference between a fix and a blank overlay.
 */

import { Loader2, Monitor } from "lucide-react"

import { Button } from "@/components/ui/button"
import { templatePlatform } from "@/constants"
import { nodeOpsInFlight } from "@/lib/staging"
import { useConsoleStore } from "@/store/console"
import { useStagingStore } from "@/store/staging"
import { useTopologyStore } from "@/store/topology"

export function RemoteDesktopPanel({ nodeId }: { nodeId: string }) {
  const data = useTopologyStore(
    (s) => s.nodes.find((n) => n.id === nodeId)?.data,
  )
  // Per-node, not the plan-wide `deploying` flag. A deploy holds one lock
  // over a DAG of many machines, and this node's own steps are usually done
  // long before the plan is: gating on the global flag meant a lab that had
  // finished standing up a DC could not be opened until the last CA in the
  // plan had also finished, which is exactly when someone wants to look at it.
  const opsInFlight = useStagingStore((s) => nodeOpsInFlight(s.ops, nodeId))
  const start = useConsoleStore((s) => s.start)
  const authorizing = useConsoleStore(
    (s) => s.session?.nodeId === nodeId && s.session.status === "authorizing",
  )

  // `vmName` is the "a real VM exists" signal, and an address is what guacd
  // dials; without both there is nothing to connect to.
  if (!data?.vmName || !data.ip) return null
  // No RDP on the Linux product templates, and no credential was ever set for
  // one — the backend would 409, so do not offer the button at all.
  if (templatePlatform(data.typeId) === "linux") return null

  const vmName = data.vmName
  const label = data.name

  return (
    <section className="flex flex-col gap-2 border-t pt-3">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        Remote Desktop
      </p>
      <Button
        variant="outline"
        size="sm"
        className="w-full justify-start gap-2"
        disabled={authorizing || opsInFlight}
        onClick={() => start({ nodeId, vmName, label })}
      >
        {authorizing ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Monitor className="h-3.5 w-3.5" />
        )}
        {authorizing ? "Connecting…" : "Connect"}
      </Button>
      <p className="text-[11px] leading-snug text-muted-foreground">
        {opsInFlight
          ? "A deployment step is still running on this machine — the desktop opens once it finishes."
          : "Opens the guest desktop in this tab. Signed in for you — no password needed."}
      </p>
    </section>
  )
}
