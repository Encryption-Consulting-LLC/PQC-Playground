import { describe, expect, it } from "vitest"

import { formatElapsed } from "@/lib/duration"

describe("formatElapsed", () => {
  it("reports bare seconds under a minute", () => {
    expect(formatElapsed(0)).toBe("0s")
    expect(formatElapsed(45)).toBe("45s")
    expect(formatElapsed(59)).toBe("59s")
  })

  it("adds minutes at the minute boundary", () => {
    expect(formatElapsed(60)).toBe("1m 0s")
    expect(formatElapsed(252)).toBe("4m 12s")
    expect(formatElapsed(3599)).toBe("59m 59s")
  })

  it("adds hours at the hour boundary", () => {
    expect(formatElapsed(3600)).toBe("1h 0m 0s")
    expect(formatElapsed(3852)).toBe("1h 4m 12s")
    expect(formatElapsed(45296)).toBe("12h 34m 56s")
  })

  it("keeps trailing zero units so a ticking clock holds its width", () => {
    expect(formatElapsed(120)).toBe("2m 0s")
    expect(formatElapsed(7200)).toBe("2h 0m 0s")
  })

  it("clamps a clock that steps backwards and floors fractions", () => {
    expect(formatElapsed(-5)).toBe("0s")
    expect(formatElapsed(59.9)).toBe("59s")
  })
})
