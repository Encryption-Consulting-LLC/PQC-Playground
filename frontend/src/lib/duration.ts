/**
 * Elapsed-time formatting for the deploy panel.
 *
 * Deliberately separate from `DeployCompilerDialog`'s local `duration()`, which
 * formats *estimates*: that one rounds minutes up and drops seconds, because a
 * predicted "12m" is more honest than a predicted "11m 43s". This one reports
 * something that actually happened, so it keeps every unit down to the second.
 */

/**
 * Render whole seconds, scaling the unit to the magnitude: `45s`, `4m 12s`,
 * `1h 4m 12s`.
 *
 * Smaller units are kept once a larger one appears (`2m 0s`, not `2m`) so a
 * ticking clock doesn't change width as it crosses a boundary. Negative and
 * fractional inputs are clamped and floored — a clock driven by wall time can
 * briefly read backwards if the system clock steps.
 */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}
