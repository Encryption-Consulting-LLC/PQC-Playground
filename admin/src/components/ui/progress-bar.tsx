/**
 * Thin inline progress bar: a track that fills left-to-right with the
 * percentage to its right, vertically centered against the (small) bar.
 *
 * Ported from the operator app's canvas ProgressBar, with its bare `gap-2` and
 * arbitrary `text-[10px]` replaced by tokens — tokens.css forbids literals.
 */

import { cn } from "@/lib/utils"

export function ProgressBar({ pct, className }: { pct: number; className?: string }) {
  const clamped = Math.max(0, Math.min(100, pct))
  return (
    <div
      className={cn(
        "flex w-full min-w-0 items-center gap-(--gap-inline) overflow-hidden",
        className,
      )}
    >
      <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-200"
          style={{ width: `${clamped}%` }}
        />
      </div>
      <span className="w-9 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
        {Math.round(clamped)}%
      </span>
    </div>
  )
}
