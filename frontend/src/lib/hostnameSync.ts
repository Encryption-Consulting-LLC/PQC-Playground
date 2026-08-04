import { ISO_MODES } from "@/constants/iso"
import { templatePlatform } from "@/constants/templates"
import { generateHostname } from "@/lib/api"
import { defaultHostname, hostnameScriptName } from "@/lib/hostname"
import { useTopologyStore } from "@/store/topology"

/**
 * Re-render a node's authored `10-hostname` script for a new node name, so the
 * name at the top of the Inspector and the hostname the guest actually boots
 * with can't drift apart. Called from the Inspector's rename — the only path
 * that changes a node's name once its ISO has been seeded.
 *
 * One-way on purpose: the node name is the source of truth, so this overwrites
 * a hand-edited hostname script. Editing that script does not rename the node.
 * A no-op when there's no authored hostname file — which is every guest (the
 * panel is operator-only), and there the server derives the hostname from the
 * namespaced VM name anyway, so it is already in step.
 */
export async function syncHostnameScript(nodeId: string, nodeName: string) {
  const node = useTopologyStore.getState().nodes.find((n) => n.id === nodeId)
  const iso = node?.data.isoAuthoring
  if (!node || !iso?.enabled || iso.mode !== ISO_MODES.pack) return

  const platform = templatePlatform(node.data.typeId)
  const scriptName = hostnameScriptName(platform)
  if (!iso.files.some((file) => file.name === scriptName)) return

  try {
    const content = await generateHostname({
      platform,
      hostname: defaultHostname(nodeName, platform),
    })
    // Re-read: the panel may have been edited, or the toggle flipped, in flight.
    const current = useTopologyStore
      .getState()
      .nodes.find((n) => n.id === nodeId)?.data.isoAuthoring
    if (!current?.enabled) return
    if (!current.files.some((file) => file.name === scriptName)) return
    useTopologyStore.getState().setIsoAuthoring(nodeId, {
      files: current.files.map((file) =>
        file.name === scriptName ? { ...file, content } : file,
      ),
    })
  } catch {
    // Keep the previous script rather than blanking it; the operator can
    // regenerate from the panel, and the server renders its own as a fallback.
  }
}
