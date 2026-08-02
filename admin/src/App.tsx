import { RouterProvider } from "@tanstack/react-router"
import { ShieldAlert } from "lucide-react"

import { ROLES } from "@/constants"
import { useAuthStore } from "@/store/auth"
import { LoginForm } from "@/components/LoginForm"
import { LogoutButton } from "@/components/LogoutButton"
import { Splash } from "@/components/Splash"
import { useApplyTheme } from "@/hooks/useTheme"
import { useMe } from "@/hooks/useMe"
import { router } from "@/router"

/**
 * The auth gate sits *above* the router rather than inside it as route
 * guards, and that is what preserves the URL across a lapsed session: while
 * signed out the router is never mounted, so nothing navigates. `lib/api.ts`
 * clears the store on a 401, this flips to the login form, and the address
 * bar still reads wherever the admin was. Signing back in mounts the router,
 * which reads `window.location` and lands there — no `?redirect=` param, and
 * no open-redirect validation to get wrong.
 */
function App() {
  // Apply the resolved theme to <html> on every render, before any early
  // return, so the login/splash/denied screens are themed too.
  useApplyTheme()

  const token = useAuthStore((s) => s.token)

  // Called unconditionally (before any early return) so hook order stays
  // stable across renders — useMe() internally no-ops via `enabled: !!token`
  // until a session exists.
  const me = useMe()

  if (!token) return <LoginForm />
  if (!me) return <Splash label="Loading session…" />

  if (me.role !== ROLES.admin) {
    return (
      <div className="flex min-h-svh flex-col items-center justify-center gap-(--gap-row) px-(--pad-page) text-center">
        <ShieldAlert className="size-8 text-warning" />
        <p className="text-sm font-medium">This console is for admins only.</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Your account ({me.username}) doesn&apos;t have admin access. Sign out and use an admin
          account, or ask an admin to provision one for you.
        </p>
        <LogoutButton />
      </div>
    )
  }

  return <RouterProvider router={router} />
}

export default App
