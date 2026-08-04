import type { Platform } from "@/lib/api"

/** Disc filename of the firstboot hostname script, per platform. */
export const HOSTNAME_BASENAME = "10-hostname"

export function hostnameScriptName(platform: Platform): string {
  return `${HOSTNAME_BASENAME}.${platform === "linux" ? "sh" : "ps1"}`
}

/**
 * Frontend mirror of the backend's `hostname_for` / `linux_hostname_for`
 * (`core/firstboot.py`).
 *
 * Windows: drop the `guest-` literal and the per-user segment of a namespaced
 * name (they don't distinguish hosts on the shared guest subnet), then keep the
 * *head* of what's left inside the 15-char NetBIOS limit — so the fixed-width
 * project code survives and only a long machine name is clipped. Linux keeps
 * the full namespace, lowercased, to 63.
 *
 * Kept in step with the backend deliberately: the operator's authored
 * `10-hostname` script and the sequence engine's `NodeContext.hostname` must
 * agree, or a DC promotes under a different name than DNS was told about.
 * Still only a prefill — configgen validates the real value on generate.
 */
export function defaultHostname(nodeName: string, platform: Platform): string {
  const safe =
    nodeName.replace(/[^A-Za-z0-9-]/g, "-").replace(/^-+|-+$/g, "") || "vm"
  if (platform === "linux") {
    return (
      safe
        .toLowerCase()
        .slice(0, 63)
        .replace(/^-+|-+$/g, "") || "vm"
    )
  }
  const parts = safe.split("-")
  const tail = parts[0] === "guest" && parts.length > 2 ? parts.slice(2) : parts
  return (
    tail
      .join("-")
      .slice(0, 15)
      .replace(/^-+|-+$/g, "") || "vm"
  )
}
