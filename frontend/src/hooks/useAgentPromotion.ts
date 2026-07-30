/**
 * Bridges live orchestrator presence (`store/agents.ts`) to node lifecycle:
 * the moment a node's baked agent first phones home, the node is promoted from
 * `provisioning` to `deployed` — the confirmation that turns its dashed domain
 * circle solid and reveals its IP.
 *
 * A cloned VM is "confirmed deployed" only once the in-guest agent is actually
 * up, not merely when vmkit reports the clone finished. `applyPlanState` parks
 * such nodes in `provisioning`; this hook (mounted once for the workspace)
 * watches the presence snapshot and promotes them when their vm_id appears.
 * It also covers the race where the agent phones home *after* the clone's
 * done-frame arrived (the same-frame case is handled inline in staging).
 */

import { useEffect } from "react"

import { LIFECYCLE } from "@/constants/topology"
import { nodeAwaitingRealization } from "@/lib/staging"
import { useAgentsStore } from "@/store/agents"
import { useStagingStore } from "@/store/staging"
import { useTopologyStore } from "@/store/topology"

export function useAgentPromotion(): void {
  const onlineVmIds = useAgentsStore((s) => s.onlineVmIds)
  const nodes = useTopologyStore((s) => s.nodes)
  const ops = useStagingStore((s) => s.ops)

  useEffect(() => {
    if (onlineVmIds.length === 0) return
    const online = new Set(onlineVmIds)
    const { promoteProvisioned } = useTopologyStore.getState()
    for (const n of nodes) {
      if (
        n.data.lifecycle === LIFECYCLE.provisioning &&
        n.data.executorVmId &&
        online.has(n.data.executorVmId) &&
        // Presence proves the VM booted, not that its role landed — while the
        // node's provision/realization ops are unresolved, the plan stream
        // (`applyPlanState`) owns the lifecycle.
        !nodeAwaitingRealization(ops, n.id)
      ) {
        promoteProvisioned(n.id)
      }
    }
  }, [onlineVmIds, nodes, ops])
}
