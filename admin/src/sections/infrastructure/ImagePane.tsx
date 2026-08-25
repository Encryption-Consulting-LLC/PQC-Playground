import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useInfraForm } from "./context"

export function ImagePane() {
  const { form, patch } = useInfraForm()

  return (
    <>
      <div className="grid gap-(--gap-row) sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="clone-base">Datastore image name</Label>
          <Input id="clone-base" value={form.cloneBase} onChange={(e) => patch("cloneBase", e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="clone-datastore">Datastore</Label>
          <Input id="clone-datastore" value={form.cloneDatastore} onChange={(e) => patch("cloneDatastore", e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="clone-guest-os">Expected VMware guest OS</Label>
          <Input id="clone-guest-os" value={form.cloneGuestOs} onChange={(e) => patch("cloneGuestOs", e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="clone-usage">Maximum datastore usage (%)</Label>
          <Input id="clone-usage" type="number" min="1" max="100" step="0.1" value={form.cloneMaxUsagePct} onChange={(e) => patch("cloneMaxUsagePct", e.target.value)} />
        </div>
        <div className="space-y-1.5 sm:col-span-2">
          <Label htmlFor="clone-admin-password">Image Administrator password</Label>
          <Input id="clone-admin-password" type="password" autoComplete="off" value={form.cloneAdminPassword} onChange={(e) => patch("cloneAdminPassword", e.target.value)} placeholder={form.hasCloneAdminPassword ? "Saved — enter to replace" : "Not recorded"} />
        </div>
      </div>
      <p className="mt-(--gap-row) text-xs text-muted-foreground">
        The built-in <code>Administrator</code> password baked into the image before sysprep. Every
        cloned guest keeps it, so this is what Remote Desktop signs in with — record the image's
        real password here rather than choosing a new one. A domain controller is the exception and
        uses the domain administrator password set on its node. Leave this empty and Remote Desktop
        is unavailable on everything else; the guests are otherwise unaffected.
      </p>
    </>
  )
}
