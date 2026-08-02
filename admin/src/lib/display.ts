/**
 * Presentation helpers shared across sections.
 *
 * Each of these was copy-pasted per section until a third copy appeared. They
 * are here so a timestamp or an error toast reads identically wherever it
 * shows up — an admin comparing two tables shouldn't have to wonder whether
 * two differently-formatted dates mean different things.
 */

import { toast } from "sonner"

import { ApiError } from "@/lib/api"

/** Epoch milliseconds → local string; an em dash for "never" / "unknown". */
export function formatDate(ms: number | null | undefined): string {
  if (!ms) return "—"
  return new Date(ms).toLocaleString()
}

/**
 * Toast an API failure with its status, so a 409 (refused on purpose) is
 * visibly different from a 500 (something broke).
 */
export const showError = (err: unknown) =>
  toast.error(err instanceof ApiError ? `${err.status}: ${err.message}` : String(err))

export type StatusVariant = "success" | "warning" | "destructive" | "outline"

/** Registry status → badge tone. */
export function statusVariant(status: string): StatusVariant {
  switch (status) {
    case "ready":
      return "success"
    case "cloning":
      return "warning"
    case "error":
      return "destructive"
    default:
      return "outline"
  }
}

/** "1 VM" / "3 VMs" — pluralisation that reads naturally in confirmation copy. */
export function plural(count: number, singular: string, pluralForm?: string): string {
  return `${count} ${count === 1 ? singular : (pluralForm ?? `${singular}s`)}`
}
