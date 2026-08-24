import {
  KeyRound,
  Network,
  Rocket,
  Server,
  ShieldAlert,
  Users2,
} from "lucide-react"
import type { LinkProps } from "@tanstack/react-router"
import type { LucideIcon } from "lucide-react"

export interface NavItem {
  /**
   * Typed against the registered route tree, so a mistyped path is a compile
   * error rather than a nav item that silently never lights up.
   */
  to: NonNullable<LinkProps["to"]>
  label: string
  icon: LucideIcon
}

/**
 * The sidebar, and the source of the header title.
 *
 * Paths follow the visible labels rather than the internal section ids the
 * console used before it had a router (`/accounts`, not `/users`) — these are
 * addresses people paste to each other, so they should read like the thing
 * they point at.
 *
 * Every path here becomes a top-level segment under `/admin`, which the
 * backend only falls back to `index.html` for when no real file matches
 * (`_mount_admin`). `assets`, `index.html`, `favicon.svg`, `ec-logo.png` and
 * the `ec-*-favicon-*.png` set are real files in `admin/dist` and would
 * shadow a route of the same name.
 */
export const SECTIONS: NavItem[] = [
  { to: "/accounts", label: "Accounts", icon: Users2 },
  { to: "/infrastructure", label: "Infrastructure", icon: Server },
  { to: "/deployments", label: "Deployments", icon: Rocket },
  { to: "/ip-pool", label: "IP Pool", icon: Network },
  { to: "/labs", label: "Labs", icon: KeyRound },
  { to: "/registry", label: "VM Registry", icon: ShieldAlert },
]
