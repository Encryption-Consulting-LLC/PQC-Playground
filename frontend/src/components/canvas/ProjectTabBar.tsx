import { useEffect, useState } from "react"
import { KeyRound, Plus, Save, X } from "lucide-react"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useProjectsStore } from "@/store/projects"
import { useStagingStore } from "@/store/staging"
import { ProjectDeleteDialog } from "./ProjectDeleteDialog"
import { ProjectShareControl } from "./ProjectShareControl"
import { LabJoinDialog } from "./LabJoinDialog"
import { useCan } from "@/hooks/useCan"
import { useJoinedLab } from "@/hooks/useJoinedLab"
import { useAuthStore } from "@/store/auth"
import { CAPABILITIES, ROLES } from "@/constants"

export function ProjectTabBar() {
  const projects = useProjectsStore((s) => s.projects)
  const activeProjectId = useProjectsStore((s) => s.activeProjectId)
  const switchProject = useProjectsStore((s) => s.switchProject)
  const renameProject = useProjectsStore((s) => s.renameProject)
  const addProject = useProjectsStore((s) => s.addProject)
  const deleteProject = useProjectsStore((s) => s.deleteProject)
  const saveActiveSnapshot = useProjectsStore((s) => s.saveActiveSnapshot)
  const deploying = useStagingStore((s) => s.deploying)
  const isGuest = useAuthStore((s) => s.role === ROLES.guest)
  const canJoinLab = useCan(CAPABILITIES.labJoin)
  // Sharing publishes the *active* project, so it has no meaning on a lab whose
  // snapshot belongs to whoever deployed it.
  const joinedLab = useJoinedLab()
  const isActiveDirty =
    projects.find((p) => p.id === activeProjectId)?.dirty ?? false

  const [joinOpen, setJoinOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftName, setDraftName] = useState("")
  // Project pending deletion — drives the confirm dialog.
  const [pendingDelete, setPendingDelete] = useState<{
    id: string
    name: string
    joinedLab: boolean
  } | null>(null)

  useEffect(() => {
    function handler(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault()
        saveActiveSnapshot()
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [saveActiveSnapshot])

  function startEditing(id: string, name: string) {
    setEditingId(id)
    setDraftName(name)
  }

  function commitEditing() {
    if (editingId) renameProject(editingId, draftName)
    setEditingId(null)
  }

  function confirmDelete() {
    if (pendingDelete) deleteProject(pendingDelete.id)
    setPendingDelete(null)
  }

  return (
    <div className="flex h-9 shrink-0 items-center gap-1 border-b bg-muted/30 px-2">
      {projects.map((project) => {
        const isActive = project.id === activeProjectId
        const isEditing = editingId === project.id

        if (isEditing) {
          return (
            <Input
              key={project.id}
              autoFocus
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              onBlur={commitEditing}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitEditing()
                if (e.key === "Escape") setEditingId(null)
              }}
              className="h-7 w-32 text-xs"
            />
          )
        }

        return (
          <div
            key={project.id}
            className={cn(
              "group flex h-7 items-center rounded-[min(var(--radius-md),12px)] pr-1 transition-colors",
              isActive
                ? "bg-secondary text-secondary-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            <button
              type="button"
              onClick={() => switchProject(project.id)}
              onDoubleClick={() => startEditing(project.id, project.name)}
              className="h-full rounded-[inherit] pl-2.5 pr-1.5 text-xs font-medium whitespace-nowrap outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              {project.name}
              {project.joinedLab && (
                <span
                  className="ml-1 rounded-sm bg-emerald-500/15 px-1 text-[10px] font-semibold text-emerald-600 dark:text-emerald-400"
                  title="Joined lab — view and remote desktop only"
                >
                  LAB
                </span>
              )}
              {project.dirty && (
                <span
                  className="ml-1 text-muted-foreground"
                  aria-label="Unsaved changes"
                >
                  *
                </span>
              )}
            </button>
            <button
              type="button"
              disabled={deploying}
              onClick={() =>
                setPendingDelete({
                  id: project.id,
                  name: project.name,
                  joinedLab: !!project.joinedLab,
                })
              }
              aria-label={
                project.joinedLab
                  ? `Close ${project.name}`
                  : `Delete ${project.name}`
              }
              title={project.joinedLab ? "Close lab" : "Delete project"}
              className={cn(
                "flex h-4 w-4 items-center justify-center rounded-sm outline-none transition-colors",
                "opacity-0 focus-visible:opacity-100 group-hover:opacity-100",
                "hover:bg-foreground/10 hover:text-foreground",
                "disabled:cursor-not-allowed disabled:opacity-0",
                isActive && "opacity-60",
              )}
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )
      })}

      <Button
        variant="ghost"
        size="icon-sm"
        onClick={() => addProject()}
        aria-label="New project"
      >
        <Plus />
      </Button>

      {canJoinLab && (
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => setJoinOpen(true)}
          aria-label="Join a lab"
          title="Join a lab with a code"
        >
          <KeyRound />
        </Button>
      )}

      <Button
        variant="ghost"
        size="icon-sm"
        className="ml-auto"
        disabled={!isActiveDirty || !!joinedLab}
        onClick={() => saveActiveSnapshot()}
        aria-label="Save project (Ctrl+S)"
        title="Save project (Ctrl+S)"
      >
        <Save />
      </Button>

      {isGuest && !joinedLab && <ProjectShareControl />}

      <LabJoinDialog open={joinOpen} onOpenChange={setJoinOpen} />

      <ProjectDeleteDialog
        projectName={pendingDelete?.name ?? null}
        joinedLab={pendingDelete?.joinedLab ?? false}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  )
}
