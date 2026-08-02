/**
 * How each orphan signal is presented.
 *
 * A row can fire several at once and every one of them is shown — they are
 * different reasons, not a severity ladder, and an admin deciding whether to
 * destroy something wants all of them.
 */

import { AlertTriangle, Hourglass, PlugZap, ServerCrash, type LucideIcon } from "lucide-react"

import type { OrphanSignal } from "@/lib/api"

export interface SignalMeta {
  label: string
  variant: "destructive" | "warning"
  icon: LucideIcon
  help: string
}

export const SIGNAL_META: Record<OrphanSignal, SignalMeta> = {
  absentFromEsxi: {
    label: "absent on ESXi",
    variant: "destructive",
    icon: ServerCrash,
    help: "The registry has an entry, but no VM by that name exists on the host.",
  },
  agentDead: {
    label: "agent dead",
    variant: "warning",
    icon: PlugZap,
    help: "The in-guest agent has not phoned home for longer than the configured threshold.",
  },
  statusError: {
    label: "errored",
    variant: "destructive",
    icon: AlertTriangle,
    help: "The clone failed. The VM may or may not exist, and may still hold an address.",
  },
  stuckCloning: {
    label: "stuck cloning",
    variant: "warning",
    icon: Hourglass,
    help: "Still marked as cloning long after its last update — the clone never finished or errored.",
  },
}
