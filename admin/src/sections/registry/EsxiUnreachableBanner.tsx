import { CloudOff } from "lucide-react"

/**
 * Shown whenever the backend could not read the ESXi inventory.
 *
 * These views deliberately keep rendering without the host — an admin
 * diagnosing a broken playground is exactly who needs them — but two things
 * silently stop working: "absent from ESXi" cannot be computed, and the purge
 * interlock (which depends on proving a row backs no VM) refuses everything.
 * Saying so beats a table that looks complete and quietly isn't.
 */
export function EsxiUnreachableBanner() {
  return (
    <div className="flex gap-(--gap-inline) rounded-lg bg-warning/10 p-(--pad-control) text-xs text-warning">
      <CloudOff className="mt-0.5 size-3.5 shrink-0" />
      <span>
        The ESXi host could not be reached, so nothing here can be checked against real
        inventory. Missing machines are not flagged, and destroy and removal actions will
        refuse until the host is reachable again.
      </span>
    </div>
  )
}
