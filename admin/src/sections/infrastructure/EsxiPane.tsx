import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useInfraForm } from "./context"

export function EsxiPane() {
  const { form, patch } = useInfraForm()

  return (
    <div className="grid gap-(--gap-row) sm:grid-cols-2">
      <div className="space-y-1.5">
        <Label htmlFor="esxi-host">Host</Label>
        <Input id="esxi-host" value={form.esxiHost} onChange={(e) => patch("esxiHost", e.target.value)} placeholder="192.168.100.10" />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="esxi-port">Port</Label>
        <Input id="esxi-port" type="number" min="1" max="65535" value={form.esxiPort} onChange={(e) => patch("esxiPort", e.target.value)} />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="esxi-user">Username</Label>
        <Input id="esxi-user" value={form.esxiUser} onChange={(e) => patch("esxiUser", e.target.value)} placeholder="root" />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="esxi-password">Password</Label>
        <Input id="esxi-password" type="password" value={form.esxiPassword} onChange={(e) => patch("esxiPassword", e.target.value)} placeholder={form.hasPassword ? "Saved — enter to replace" : "Required"} />
      </div>
    </div>
  )
}
