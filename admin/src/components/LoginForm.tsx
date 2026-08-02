import { useEffect, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  ApiError,
  login,
  oidcCallback,
  oidcLoginUrl,
  type LoginRequest,
  type SessionResponse,
} from "@/lib/api"
import { OIDC_CALLBACK } from "@/lib/oidcCallback"
import { useAuthStore } from "@/store/auth"
import { Button } from "@/components/ui/button"
import { FloatingField } from "@/components/ui/floating-field"
import { Splash } from "@/components/Splash"
import { ThemeToggle } from "@/components/ThemeToggle"
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card"

function storeSession(s: SessionResponse) {
  useAuthStore.getState().setSession({
    token: s.token,
    username: s.username,
    role: s.role,
    host: s.host,
  })
}

let exchangeStarted = false

const showError = (err: unknown) =>
  toast.error(err instanceof ApiError ? `${err.status}: ${err.message}` : String(err))

/**
 * Admin login screen — same POST /auth/login + OIDC code-flow convention as
 * frontend/src/components/LoginForm.tsx. Any account can sign in here; role
 * is checked afterward by App.tsx (an "operators only" screen for anyone
 * else) — the backend independently 403s every admin route regardless.
 */
export function LoginForm() {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")

  const loginMutation = useMutation({
    mutationFn: (req: LoginRequest) => login(req),
    onSuccess: storeSession,
    onError: showError,
  })

  const ssoStart = useMutation({
    mutationFn: oidcLoginUrl,
    onSuccess: ({ url }) => {
      window.location.assign(url)
    },
    onError: showError,
  })

  const ssoFinish = useMutation({
    mutationFn: ({ code, state }: { code: string; state: string }) =>
      oidcCallback(code, state),
    onSuccess: storeSession,
    onError: showError,
  })

  // Returning leg of the SSO redirect. The params were already read off the
  // URL and scrubbed at module load (see lib/oidcCallback) — all that's left
  // is to redeem them. The guard is module-scope rather than a ref because a
  // consumed code must not be replayed by StrictMode's double-invoked effect,
  // nor by a remount of this component.
  useEffect(() => {
    if (!OIDC_CALLBACK || exchangeStarted) return
    exchangeStarted = true
    ssoFinish.mutate(OIDC_CALLBACK)
  }, [ssoFinish])

  if (ssoFinish.isPending) return <Splash label="Completing SSO sign-in…" />

  return (
    <div className="login-bg relative flex min-h-svh items-center justify-center px-(--pad-page) py-10">
      {/* `!absolute` overrides the `.login-bg > *` rule, which forces
          position:relative on every direct child (for the glow z-stacking). */}
      <div className="!absolute top-4 right-4 !z-20">
        <ThemeToggle />
      </div>
      <div className="login-card-border w-full max-w-sm">
        <Card className="login-card w-full ring-0 [--card-spacing:--spacing(6)]">
          <CardHeader className="items-center justify-items-center gap-5 text-center">
            <img
              src={`${import.meta.env.BASE_URL}ec-logo.png`}
              alt="Encryption Consulting"
              className="ec-logo max-w-[9rem]"
            />
            <CardDescription>Sign in to the PQC Playground admin console</CardDescription>
          </CardHeader>
          <CardContent>
            <form
              className="grid gap-5"
              onSubmit={(e) => {
                e.preventDefault()
                loginMutation.mutate({ username, password })
              }}
            >
              <FloatingField
                id="login-username"
                label="Username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                autoFocus
              />

              <FloatingField
                id="login-password"
                label="Password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />

              <Button type="submit" className="mt-1 w-full" disabled={loginMutation.isPending}>
                {loginMutation.isPending ? "Signing in…" : "Sign in"}
              </Button>
            </form>

            <div className="my-5 flex items-center gap-(--gap-row) text-xs text-muted-foreground">
              <div className="h-px flex-1 bg-border" />
              or
              <div className="h-px flex-1 bg-border" />
            </div>

            <Button
              type="button"
              variant="outline"
              className="w-full"
              disabled
              title="SSO sign-in is not yet available"
            >
              {ssoStart.isPending ? "Redirecting…" : "Sign in with SSO"}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
