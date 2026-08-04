import { useEffect, useRef, useState } from "react"
import {
  Disc3,
  FileCode2,
  Loader2,
  Lock,
  Plus,
  Upload,
  Wand2,
  X,
} from "lucide-react"
import { toast } from "sonner"

import {
  ISO_FILE_MAX_BYTES,
  ISO_FILE_NAME_RE,
  ISO_MAX_FILES,
  ISO_MODES,
  ISO_OP_MAX_BYTES,
  ISO_UPLOAD_MAX_BYTES,
} from "@/constants/iso"
import type { IsoMode } from "@/constants/iso"
import {
  deleteIso,
  generateHostname,
  getTemplateScripts,
  uploadIso,
} from "@/lib/api"
import { cn } from "@/lib/utils"
import { defaultHostname, hostnameScriptName } from "@/lib/hostname"
import { seedsFirstbootScripts, templatePlatform } from "@/constants/templates"
import { LIFECYCLE } from "@/constants/topology"
import type { IsoAuthoring, IsoFileEntry } from "@/store/topology"
import { useTopologyStore } from "@/store/topology"
import { useStagingStore } from "@/store/staging"
import { Button } from "@/components/ui/button"
import { GenerateScriptDialog } from "./GenerateScriptDialog"
import { ScriptEditorDialog } from "./ScriptEditorDialog"

const EMPTY_ISO: IsoAuthoring = {
  enabled: false,
  mode: ISO_MODES.pack,
  files: [],
}

function totalBytes(files: IsoFileEntry[]): number {
  const enc = new TextEncoder()
  return files.reduce((sum, f) => sum + enc.encode(f.content).length, 0)
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
  return `${Math.max(1, Math.round(bytes / 1024))} KiB`
}

/**
 * The rest of the real firstboot disc — the parts the *server* owns, listed so
 * the panel mirrors `core/firstboot.py`'s `10-/20-/40-/50-` manifest instead of
 * implying the authored files are all of it.
 *
 * These aren't authored files because the client cannot produce them honestly:
 * `20-network` bakes the IP claimed from the guest pool at clone time, and the
 * agent's install step plus its bearer token are minted per VM. The DC's
 * password reset is listed because it has to precede `Install-ADDSForest` and so
 * cannot be an executor step.
 */
function serverManagedScripts(
  templateId: string,
): { name: string; detail: string }[] {
  const platform = templatePlatform(templateId)
  const extension = platform === "linux" ? "sh" : "ps1"
  const rows = [
    {
      name: `20-network.${extension}`,
      detail: "Static address claimed from the guest IP pool at deploy",
    },
  ]
  if (platform === "windows") {
    rows.push({
      name: "40-install-executor.ps1",
      detail: "Phone-home agent — always installed, cannot be removed",
    })
  }
  if (templateId === "domainController") {
    rows.push({
      name: "50-password.ps1",
      detail: "Resets the local Administrator to this DC's domain password",
    })
  }
  return rows
}

/** First `new-script.ps1` variant that doesn't collide with existing names. */
function freshName(existing: string[], extension: ".ps1" | ".sh"): string {
  const initial = `new-script${extension}`
  if (!existing.includes(initial)) return initial
  for (let i = 2; ; i++) {
    const candidate = `new-script-${i}${extension}`
    if (!existing.includes(candidate)) return candidate
  }
}

/**
 * Operator-only "Include ISO" section of the Inspector. Behind the
 * toggle sit two modes: PACK — a file-manager-style grid of authored firstboot
 * scripts (double-click to edit; backend packs them with isokit at deploy
 * time) — and UPLOAD-ISO — a pre-built .iso pushed to the backend now and
 * attached verbatim at deploy time.
 *
 * The two modes differ in how much of the disc they own. PACK files are
 * *layered over* the rendered set (`build_firstboot_iso`'s `authored_scripts`):
 * a same-named file wins, anything else is appended, and the pool IP, the agent
 * and a DC's password reset are injected regardless — which is why those appear
 * here as read-only server-managed rows. UPLOAD-ISO is the complete disc: the
 * server injects nothing and the VM gets no pool IP.
 *
 * For a PKI component template (`seedsFirstbootScripts`) the panel opens
 * pre-filled rather than off-and-empty, so the first thing an operator sees is
 * the disc that will actually boot.
 *
 * All durable state lives on the node (`MachineData.isoAuthoring`) so it rides
 * project snapshots; deploy reads it fresh (`buildOpPayload`), so edits on a
 * staged node need no restage.
 */
export function IsoAuthoringPanel({ nodeId }: { nodeId: string }) {
  const node = useTopologyStore((s) => s.nodes.find((n) => n.id === nodeId))
  const setIsoAuthoring = useTopologyStore((s) => s.setIsoAuthoring)
  const deploying = useStagingStore((s) => s.deploying)

  const [editing, setEditing] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  const [uploading, setUploading] = useState(false)
  const scriptInputRef = useRef<HTMLInputElement>(null)
  const isoInputRef = useRef<HTMLInputElement>(null)

  // Pre-fill a component template's disc the first time its Inspector is opened
  // (the panel only mounts on selection, which is exactly the "placed, then
  // clicked" moment). `seeded` is written up front so a re-render mid-fetch —
  // or reopening the node — can't fire this twice. Only the hostname script is
  // authored; see `serverManagedScripts` for why the rest can't be.
  useEffect(() => {
    if (!node || deploying) return
    const { typeId, name, lifecycle, isoAuthoring } = node.data
    if (isoAuthoring?.seeded || !seedsFirstbootScripts(typeId)) return
    // Never seed a realized node: `setIsoAuthoring` would flag it as drifted.
    // `staged` is included so a node from the PKI starter template — which
    // arrives pre-configured — gets the same disc as a hand-dropped one.
    if (
      lifecycle !== LIFECYCLE.draft &&
      lifecycle !== LIFECYCLE.failed &&
      lifecycle !== LIFECYCLE.staged
    ) {
      return
    }

    setIsoAuthoring(nodeId, {
      enabled: true,
      mode: ISO_MODES.pack,
      seeded: true,
    })
    const platform = templatePlatform(typeId)
    generateHostname({ platform, hostname: defaultHostname(name, platform) })
      .then((script) => {
        const current = useTopologyStore
          .getState()
          .nodes.find((n) => n.id === nodeId)?.data.isoAuthoring
        // Only fill an untouched grid — the operator may have added a file, or
        // flipped the toggle back off, while this was in flight.
        if (!current?.enabled || current.files.length > 0) return
        setIsoAuthoring(nodeId, {
          files: [{ name: hostnameScriptName(platform), content: script }],
        })
      })
      .catch(() => {
        // Seeding is a convenience; an empty grid is a fine starting point and
        // the server renders its own hostname script when none is authored.
      })
  }, [node, nodeId, deploying, setIsoAuthoring])

  if (!node) return null
  const templateId = node.data.typeId
  const iso = node.data.isoAuthoring ?? EMPTY_ISO
  const files = [...iso.files].sort((a, b) => a.name.localeCompare(b.name))
  // An authored override moves that script into the editable grid, so it stops
  // being "server-managed" — except the agent install, which always is.
  const serverManaged = serverManagedScripts(templateId).filter(
    (script) =>
      script.name === "40-install-executor.ps1" ||
      !files.some((file) => file.name === script.name),
  )
  const editingFile = editing
    ? (files.find((f) => f.name === editing) ?? null)
    : null

  function patch(next: Partial<IsoAuthoring>) {
    setIsoAuthoring(nodeId, next)
  }

  async function toggle() {
    if (deploying) return
    if (iso.enabled) {
      patch({ enabled: false })
      return
    }
    patch({ enabled: true })
    // Seed the PACK grid once with the template's fixed role scripts — the
    // same files the default path would pack, now fully editable/deletable.
    if (!iso.seeded) {
      patch({ seeded: true })
      try {
        const { scripts } = await getTemplateScripts(node!.data.typeId)
        // Re-read: the toggle may have been flipped again while fetching.
        const current = useTopologyStore
          .getState()
          .nodes.find((n) => n.id === nodeId)?.data.isoAuthoring
        if (
          current?.enabled &&
          current.files.length === 0 &&
          scripts.length > 0
        ) {
          setIsoAuthoring(nodeId, { files: scripts })
        }
      } catch {
        // Seeding is a convenience — an empty panel is a fine starting point.
      }
    }
  }

  function validateAdd(
    name: string,
    content: string,
    replacing?: string,
  ): boolean {
    if (!ISO_FILE_NAME_RE.test(name)) {
      toast.error(
        "Filename must be letters/digits/._- with a .ps1 or .sh extension.",
      )
      return false
    }
    if (name !== replacing && iso.files.some((f) => f.name === name)) {
      toast.error(`A file named "${name}" already exists.`)
      return false
    }
    const others = iso.files.filter((f) => f.name !== replacing)
    if (others.length + 1 > ISO_MAX_FILES) {
      toast.error(`At most ${ISO_MAX_FILES} files per ISO.`)
      return false
    }
    const size = new TextEncoder().encode(content).length
    if (size > ISO_FILE_MAX_BYTES) {
      toast.error(`Script exceeds ${ISO_FILE_MAX_BYTES / 1024} KiB.`)
      return false
    }
    if (totalBytes(others) + size > ISO_OP_MAX_BYTES) {
      toast.error(`Authored files exceed ${ISO_OP_MAX_BYTES / 1024} KiB total.`)
      return false
    }
    return true
  }

  /** Shared by the editor dialog (save/rename) and the panel's add paths. */
  function upsertFile(previousName: string | undefined, next: IsoFileEntry) {
    if (!validateAdd(next.name, next.content, previousName)) return
    patch({
      files: [...iso.files.filter((f) => f.name !== previousName), next],
    })
  }

  function newFile() {
    const extension = templatePlatform(templateId) === "linux" ? ".sh" : ".ps1"
    const name = freshName(
      iso.files.map((f) => f.name),
      extension,
    )
    if (!validateAdd(name, "")) return
    patch({ files: [...iso.files, { name, content: "" }] })
    setEditing(name)
  }

  /** Generated scripts replace an existing file of the same name (regenerate = update). */
  function insertGenerated(file: IsoFileEntry) {
    if (!validateAdd(file.name, file.content, file.name)) return
    const replaced = iso.files.some((f) => f.name === file.name)
    patch({ files: [...iso.files.filter((f) => f.name !== file.name), file] })
    toast.success(`${file.name} ${replaced ? "updated" : "added"}.`)
  }

  function importScript(file: File) {
    file
      .text()
      .then((content) => upsertFile(undefined, { name: file.name, content }))
      .catch(() => toast.error(`Could not read "${file.name}".`))
  }

  async function replaceUploadedIso(file: File) {
    if (file.size > ISO_UPLOAD_MAX_BYTES) {
      toast.error(`ISO exceeds ${ISO_UPLOAD_MAX_BYTES / (1024 * 1024)} MiB.`)
      return
    }
    setUploading(true)
    const previous = iso.isoId
    try {
      const uploaded = await uploadIso(file)
      setIsoAuthoring(nodeId, {
        isoId: uploaded.isoId,
        isoName: uploaded.name,
        isoSize: uploaded.size,
      })
      if (previous) deleteIso(previous).catch(() => {})
      toast.success(`"${uploaded.name}" uploaded.`)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Upload failed.")
    } finally {
      setUploading(false)
    }
  }

  function removeUploadedIso() {
    if (iso.isoId) deleteIso(iso.isoId).catch(() => {})
    patch({ isoId: undefined, isoName: undefined, isoSize: undefined })
  }

  return (
    <section className="flex flex-col gap-2">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        Config ISO
      </p>

      <button
        onClick={toggle}
        disabled={deploying}
        className={cn(
          "flex items-center gap-2 rounded-md border p-2 text-left text-xs transition-colors",
          iso.enabled
            ? "border-primary/50 bg-primary/5 text-foreground"
            : "text-muted-foreground hover:text-foreground",
          deploying && "pointer-events-none opacity-50",
        )}
      >
        <Disc3
          className={cn("h-3.5 w-3.5 shrink-0", iso.enabled && "text-primary")}
        />
        <span className="flex-1">Include ISO</span>
        <span
          className={cn(
            "relative h-4 w-7 rounded-full transition-colors",
            iso.enabled ? "bg-primary" : "bg-muted-foreground/30",
          )}
        >
          <span
            className={cn(
              "absolute top-0.5 h-3 w-3 rounded-full bg-background transition-[left]",
              iso.enabled ? "left-3.5" : "left-0.5",
            )}
          />
        </span>
      </button>

      {iso.enabled && (
        <>
          {/* Mode segmented control */}
          <div className="grid grid-cols-2 gap-0.5 rounded-md border p-0.5 text-xs">
            {(
              [
                [ISO_MODES.pack, "Pack files"],
                [ISO_MODES.uploadIso, "Upload ISO"],
              ] as [IsoMode, string][]
            ).map(([mode, label]) => (
              <button
                key={mode}
                onClick={() => patch({ mode })}
                disabled={deploying}
                className={cn(
                  "rounded px-2 py-1 transition-colors",
                  iso.mode === mode
                    ? "bg-primary/10 font-medium text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
              </button>
            ))}
          </div>

          {iso.mode === ISO_MODES.pack ? (
            <>
              <p className="text-[11px] leading-4 text-muted-foreground">
                These scripts are packed over the generated set — a file named
                after one below replaces it.
              </p>

              {/* File-manager grid: one icon per script, double-click to edit */}
              {files.length > 0 ? (
                <div className="grid grid-cols-3 gap-1">
                  {files.map((file) => (
                    <button
                      key={file.name}
                      onDoubleClick={() => setEditing(file.name)}
                      disabled={deploying}
                      title={`${file.name} — double-click to edit`}
                      className="flex flex-col items-center gap-1 rounded-md border p-2 text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                    >
                      <FileCode2 className="h-5 w-5" />
                      <span className="w-full truncate text-center text-[10px]">
                        {file.name}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="rounded-md border border-dashed p-3 text-center text-[11px] text-muted-foreground">
                  No scripts yet — add or upload one.
                </p>
              )}
              <div className="flex gap-1.5">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 flex-1 gap-1 text-xs"
                  disabled={deploying}
                  onClick={newFile}
                >
                  <Plus className="h-3 w-3" /> New file
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 flex-1 gap-1 text-xs"
                  disabled={deploying}
                  onClick={() => scriptInputRef.current?.click()}
                >
                  <Upload className="h-3 w-3" /> Upload file
                </Button>
                <input
                  ref={scriptInputRef}
                  type="file"
                  accept=".ps1,.sh,.txt"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file) importScript(file)
                    e.target.value = ""
                  }}
                />
              </div>
              <Button
                variant="outline"
                size="sm"
                className="h-7 w-full gap-1 text-xs"
                disabled={deploying}
                onClick={() => setGenerating(true)}
              >
                <Wand2 className="h-3 w-3" /> Generate from template
              </Button>

              {/* The server's half of the disc. Rows an authored file overrides
                  drop out — the editable copy in the grid above is then the
                  truth (the agent's install step is never overridable). */}
              {serverManaged.length > 0 && (
                <div className="flex flex-col gap-1 border-t pt-2">
                  <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Server-managed
                  </p>
                  {serverManaged.map((script) => (
                    <div
                      key={script.name}
                      title={script.detail}
                      className="flex items-start gap-2 rounded-md border border-dashed bg-muted/30 p-2 text-[10px] text-muted-foreground"
                    >
                      <Lock className="mt-0.5 h-3 w-3 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium">{script.name}</p>
                        <p className="leading-4">{script.detail}</p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <>
              <p className="text-[11px] leading-4 text-muted-foreground">
                An uploaded ISO is the complete disc — this VM gets no pool IP,
                no hostname/network scripts and no phone-home agent.
              </p>

              {iso.isoId ? (
                <div className="flex items-center gap-2 rounded-md border p-2 text-xs">
                  <Disc3 className="h-4 w-4 shrink-0 text-primary" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate" title={iso.isoName}>
                      {iso.isoName}
                    </p>
                    <p className="text-[10px] text-muted-foreground">
                      {formatSize(iso.isoSize ?? 0)} — attached as-is at deploy
                    </p>
                  </div>
                  <button
                    onClick={removeUploadedIso}
                    disabled={deploying || uploading}
                    className="text-muted-foreground transition-colors hover:text-foreground"
                    aria-label="Remove uploaded ISO"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ) : (
                <p className="rounded-md border border-dashed p-3 text-center text-[11px] text-muted-foreground">
                  No ISO uploaded yet.
                </p>
              )}
              <Button
                variant="outline"
                size="sm"
                className="h-7 w-full gap-1 text-xs"
                disabled={deploying || uploading}
                onClick={() => isoInputRef.current?.click()}
              >
                {uploading ? (
                  <>
                    <Loader2 className="h-3 w-3 animate-spin" /> Uploading…
                  </>
                ) : (
                  <>
                    <Upload className="h-3 w-3" />{" "}
                    {iso.isoId ? "Replace ISO" : "Upload .iso"}
                  </>
                )}
              </Button>
              <input
                ref={isoInputRef}
                type="file"
                accept=".iso"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) void replaceUploadedIso(file)
                  e.target.value = ""
                }}
              />
            </>
          )}
        </>
      )}

      <ScriptEditorDialog
        file={editingFile}
        siblings={files.filter((f) => f.name !== editing).map((f) => f.name)}
        onSave={(previousName, next) => upsertFile(previousName, next)}
        onDelete={(name) =>
          patch({ files: iso.files.filter((f) => f.name !== name) })
        }
        onClose={() => setEditing(null)}
      />
      <GenerateScriptDialog
        open={generating}
        nodeName={node.data.name}
        platform={templatePlatform(templateId)}
        onInsert={insertGenerated}
        onClose={() => setGenerating(false)}
      />
    </section>
  )
}
