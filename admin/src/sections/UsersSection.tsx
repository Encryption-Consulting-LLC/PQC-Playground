import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Boxes, KeyRound, Loader2, Plus, ShieldAlert } from "lucide-react"
import { toast } from "sonner"

import { QUERY_KEYS } from "@/constants"
import {
  createUser,
  listUsers,
  patchUser,
  PRODUCT_TEMPLATES,
  type AdminUser,
  type UserCreateRequest,
} from "@/lib/api"
import { formatDate, showError } from "@/lib/display"
import { useMe } from "@/hooks/useMe"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogPopup,
  DialogPortal,
  DialogBackdrop,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

/**
 * Guest/operator account control — the reason this app exists. Every action
 * here is the existing operator-only /api/admin/users surface with a UI on
 * top: list, create, enable/disable, role change, password reset. There is
 * deliberately no delete (matches the backend — disabling covers revocation).
 */
export function UsersSection() {
  const me = useMe()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.users,
    queryFn: listUsers,
  })

  const [createOpen, setCreateOpen] = useState(false)
  const [disableTarget, setDisableTarget] = useState<AdminUser | null>(null)
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [productsTarget, setProductsTarget] = useState<AdminUser | null>(null)

  const invalidate = () => queryClient.invalidateQueries({ queryKey: QUERY_KEYS.users })

  const patchMutation = useMutation({
    mutationFn: ({ username, body }: { username: string; body: Parameters<typeof patchUser>[1] }) =>
      patchUser(username, body),
    onSuccess: invalidate,
    onError: showError,
  })

  const createMutation = useMutation({
    mutationFn: (body: UserCreateRequest) => createUser(body),
    onSuccess: () => {
      invalidate()
      setCreateOpen(false)
      toast.success("Account created.")
    },
    onError: showError,
  })

  function toggleDisabled(user: AdminUser) {
    if (user.username === me?.username) {
      toast.error("You cannot disable your own account.")
      return
    }
    if (user.disabled) {
      patchMutation.mutate({ username: user.username, body: { disabled: false } })
    } else {
      setDisableTarget(user)
    }
  }

  function changeRole(user: AdminUser, role: "admin" | "operator" | "guest") {
    if (role === user.role) return
    patchMutation.mutate({ username: user.username, body: { role } })
  }

  return (
    <div className="space-y-(--gap-stack)">
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-(--gap-row)">
            <div>
              <CardTitle>Accounts</CardTitle>
              <CardDescription>
                Every sign-in is an admin-provisioned account — admin, operator, or guest. Disabling
                takes effect on that account&apos;s next request.
              </CardDescription>
            </div>
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="mr-1 size-3.5" />
              New account
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Loading accounts…
            </div>
          ) : isError ? (
            <p className="py-(--pad-section) text-xs text-destructive">
              {error instanceof Error ? error.message : "Could not load accounts."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Username</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Products</TableHead>
                  <TableHead>Auth</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.users.map((user) => (
                  <TableRow key={user.username}>
                    <TableCell className="font-medium">
                      {user.username}
                      {user.username === me?.username && (
                        <Badge variant="outline" className="ml-(--gap-inline)">you</Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      <Select
                        value={user.role}
                        onValueChange={(value) => changeRole(user, value as "admin" | "operator" | "guest")}
                        disabled={patchMutation.isPending}
                      >
                        <SelectTrigger size="sm">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="admin">Admin</SelectItem>
                          <SelectItem value="operator">Operator</SelectItem>
                          <SelectItem value="guest">Guest</SelectItem>
                        </SelectContent>
                      </Select>
                    </TableCell>
                    <TableCell>
                      <ProductSummary user={user} />
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{user.auth}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={user.disabled ? "destructive" : "success"}>
                        {user.disabled ? "Disabled" : "Enabled"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDate(user.createdAt)}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDate(user.updatedAt)}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-(--gap-row)">
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          title="Product access"
                          aria-label="Product access"
                          onClick={() => setProductsTarget(user)}
                        >
                          <Boxes className="size-4" />
                        </Button>
                        {user.auth !== "oidc" && (
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            title="Reset password"
                            aria-label="Reset password"
                            onClick={() => setResetTarget(user)}
                          >
                            <KeyRound className="size-4" />
                          </Button>
                        )}
                        <Switch
                          checked={!user.disabled}
                          onCheckedChange={() => toggleDisabled(user)}
                          disabled={patchMutation.isPending || user.username === me?.username}
                          aria-label={user.disabled ? "Enable account" : "Disable account"}
                        />
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <CreateUserDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onSubmit={(body) => createMutation.mutate(body)}
        pending={createMutation.isPending}
      />

      <DisableUserDialog
        user={disableTarget}
        onCancel={() => setDisableTarget(null)}
        onConfirm={() => {
          if (!disableTarget) return
          patchMutation.mutate({ username: disableTarget.username, body: { disabled: true } })
          setDisableTarget(null)
        }}
      />

      <ProductAccessDialog
        key={productsTarget?.username ?? "none"}
        user={productsTarget}
        onCancel={() => setProductsTarget(null)}
        onSubmit={(products) => {
          if (!productsTarget) return
          patchMutation.mutate(
            { username: productsTarget.username, body: { products } },
            {
              onSuccess: () => {
                setProductsTarget(null)
                toast.success("Product access updated.")
              },
            },
          )
        }}
        pending={patchMutation.isPending}
      />

      <ResetPasswordDialog
        user={resetTarget}
        onCancel={() => setResetTarget(null)}
        onSubmit={(password) => {
          if (!resetTarget) return
          patchMutation.mutate(
            { username: resetTarget.username, body: { password } },
            { onSuccess: () => setResetTarget(null) },
          )
        }}
        pending={patchMutation.isPending}
      />
    </div>
  )
}

/**
 * What the account may deploy off the product catalogue.
 *
 * Operators and admins read as "All" rather than as their stored (empty) list:
 * the grant is only consulted for guests, so showing an operator's empty array
 * as "None" would describe a restriction that does not exist.
 */
function ProductSummary({ user }: { user: AdminUser }) {
  if (user.role !== "guest") {
    return <span className="text-xs text-muted-foreground">All</span>
  }
  if (user.products.length === 0) {
    return <span className="text-xs text-muted-foreground">None</span>
  }
  return (
    <div className="flex flex-wrap gap-(--gap-inline)">
      {user.products.map((id) => (
        <Badge key={id} variant="outline">
          {PRODUCT_TEMPLATES.find((product) => product.id === id)?.label ?? id}
        </Badge>
      ))}
    </div>
  )
}

/**
 * Per-account product grant — the one part of the model that is not per-role.
 * Submits the whole set, so clearing every box revokes the catalogue; it takes
 * effect on the account's next request, like disabling and role changes.
 *
 * The caller keys this on the username so the checkboxes are seeded from
 * whichever account was opened rather than kept from the last one.
 */
function ProductAccessDialog({
  user,
  onCancel,
  onSubmit,
  pending,
}: {
  user: AdminUser | null
  onCancel: () => void
  onSubmit: (products: string[]) => void
  pending: boolean
}) {
  const [selected, setSelected] = useState<string[]>(() => user?.products ?? [])

  return (
    <Dialog open={user !== null} onOpenChange={(next) => !next && onCancel()}>
      <DialogPortal>
        <DialogBackdrop />
        <DialogPopup>
          <DialogTitle>Product access for {user?.username}</DialogTitle>
          <DialogDescription>
            {user?.role === "guest"
              ? "Unselected products are greyed out in that account's palette and refused by the deploy route."
              : "Only guest accounts are restricted — an operator or admin holds the whole catalogue regardless of what is selected here."}
          </DialogDescription>
          <DialogBody>
            <div className="grid gap-(--gap-row)">
              {PRODUCT_TEMPLATES.map((product) => (
                <Label
                  key={product.id}
                  className="flex items-center gap-(--gap-inline) text-xs font-normal"
                >
                  <Checkbox
                    checked={selected.includes(product.id)}
                    onCheckedChange={(checked) =>
                      setSelected((prev) =>
                        checked
                          ? [...prev, product.id]
                          : prev.filter((id) => id !== product.id),
                      )
                    }
                  />
                  {product.label}
                </Label>
              ))}
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={onCancel}>
              Cancel
            </Button>
            <Button size="sm" disabled={pending} onClick={() => onSubmit(selected)}>
              {pending ? <Loader2 className="size-4 animate-spin" /> : <Boxes className="size-4" />}
              Save access
            </Button>
          </DialogFooter>
        </DialogPopup>
      </DialogPortal>
    </Dialog>
  )
}

function CreateUserDialog({
  open,
  onOpenChange,
  onSubmit,
  pending,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: (body: UserCreateRequest) => void
  pending: boolean
}) {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [role, setRole] = useState<"admin" | "operator" | "guest">("guest")
  const [email, setEmail] = useState("")
  // Deny by default, deliberately: a guest provisioned in a hurry gets the PKI
  // components and nothing off the catalogue, rather than everything until
  // somebody remembers to take it back.
  const [products, setProducts] = useState<string[]>([])

  function reset() {
    setUsername("")
    setPassword("")
    setRole("guest")
    setEmail("")
    setProducts([])
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) reset()
      }}
    >
      <DialogPortal>
        <DialogBackdrop />
        <DialogPopup>
          <DialogTitle>New account</DialogTitle>
          <DialogDescription>
            Provisioned directly — there is no self-serve signup. The account can sign in
            immediately with this password.
          </DialogDescription>
          <DialogBody>
            <div className="grid gap-(--gap-inline)">
              <Label htmlFor="new-username">Username</Label>
              <Input id="new-username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
            </div>
            <div className="grid gap-(--gap-inline)">
              <Label htmlFor="new-email">Email (optional)</Label>
              <Input id="new-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="grid gap-(--gap-inline)">
              <Label htmlFor="new-password">Password</Label>
              <Input
                id="new-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="At least 8 characters"
              />
            </div>
            <div className="grid gap-(--gap-inline)">
              <Label htmlFor="new-role">Role</Label>
              <Select value={role} onValueChange={(v) => setRole(v as "admin" | "operator" | "guest")}>
                <SelectTrigger id="new-role">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="admin">Admin</SelectItem>
                  <SelectItem value="operator">Operator</SelectItem>
                  <SelectItem value="guest">Guest</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {role === "guest" && (
              <div className="grid gap-(--gap-inline)">
                <Label>Product access</Label>
                {PRODUCT_TEMPLATES.map((product) => (
                  <Label
                    key={product.id}
                    className="flex items-center gap-(--gap-inline) text-xs font-normal"
                  >
                    <Checkbox
                      checked={products.includes(product.id)}
                      onCheckedChange={(checked) =>
                        setProducts((prev) =>
                          checked
                            ? [...prev, product.id]
                            : prev.filter((id) => id !== product.id),
                        )
                      }
                    />
                    {product.label}
                  </Label>
                ))}
              </div>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={pending || username.trim().length === 0 || password.length < 8}
              onClick={() =>
                onSubmit({
                  username: username.trim(),
                  password,
                  role,
                  email: email.trim() || null,
                  products,
                })
              }
            >
              {pending ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
              Create
            </Button>
          </DialogFooter>
        </DialogPopup>
      </DialogPortal>
    </Dialog>
  )
}

function DisableUserDialog({
  user,
  onCancel,
  onConfirm,
}: {
  user: AdminUser | null
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <Dialog open={user !== null} onOpenChange={(next) => !next && onCancel()}>
      <DialogPortal>
        <DialogBackdrop />
        <DialogPopup>
          <DialogTitle className="flex items-center gap-(--gap-inline)">
            <ShieldAlert className="size-4 text-warning" />
            Disable {user?.username}?
          </DialogTitle>
          <DialogDescription>
            The account is revoked on its next request. It can be re-enabled at any time.
          </DialogDescription>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onCancel}>
              Cancel
            </Button>
            <Button variant="destructive" size="sm" onClick={onConfirm}>
              Disable account
            </Button>
          </DialogFooter>
        </DialogPopup>
      </DialogPortal>
    </Dialog>
  )
}

function ResetPasswordDialog({
  user,
  onCancel,
  onSubmit,
  pending,
}: {
  user: AdminUser | null
  onCancel: () => void
  onSubmit: (password: string) => void
  pending: boolean
}) {
  const [password, setPassword] = useState("")

  return (
    <Dialog
      open={user !== null}
      onOpenChange={(next) => {
        if (!next) {
          onCancel()
          setPassword("")
        }
      }}
    >
      <DialogPortal>
        <DialogBackdrop />
        <DialogPopup>
          <DialogTitle>Reset password for {user?.username}</DialogTitle>
          <DialogDescription>
            The new password takes effect immediately; existing sessions are unaffected until
            they expire.
          </DialogDescription>
          <DialogBody>
            <div className="grid gap-(--gap-inline)">
              <Label htmlFor="reset-password">New password</Label>
              <Input
                id="reset-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="At least 8 characters"
                autoFocus
              />
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={onCancel}>
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={pending || password.length < 8}
              onClick={() => onSubmit(password)}
            >
              {pending ? <Loader2 className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
              Reset password
            </Button>
          </DialogFooter>
        </DialogPopup>
      </DialogPortal>
    </Dialog>
  )
}
