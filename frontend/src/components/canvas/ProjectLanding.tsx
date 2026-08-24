import { useState } from "react"
import { CloudCog, FilePlus2, Network } from "lucide-react"

import { cn } from "@/lib/utils"
import { CAPABILITIES } from "@/constants"
import { useCan } from "@/hooks/useCan"
import { useProjectsStore } from "@/store/projects"
import { LabJoinDialog } from "./LabJoinDialog"

/**
 * Shown by the Workspace when there are no projects (i.e. the last one was
 * deleted). Offers the two ways to start a new project — a clean workspace or
 * the pre-configured PKI lab template — plus joining a lab that is already
 * deployed, which is the one path that builds nothing.
 */
export function ProjectLanding() {
  const addProject = useProjectsStore((s) => s.addProject)
  const addProjectFromTemplate = useProjectsStore(
    (s) => s.addProjectFromTemplate,
  )
  // Which lab a visitor lands in is decided by the code they were given, not
  // by a build-time URL: one deployment now serves as many pre-built labs as
  // there are cohorts. Gated on the capability rather than the role, since
  // operators hold it too (it is how they see what a cohort sees).
  const canJoinLab = useCan(CAPABILITIES.labJoin)
  const [joinOpen, setJoinOpen] = useState(false)

  return (
    <div className="login-bg flex flex-1 items-center justify-center overflow-y-auto p-6">
      <div className="flex w-full max-w-4xl flex-col items-center gap-8">
        <div className="flex flex-col items-center gap-1.5 text-center">
          <h2 className="text-xl font-semibold tracking-tight">
            How do you wish to start?
          </h2>
          <p className="text-sm text-muted-foreground">
            Create a project to begin composing your topology.
          </p>
        </div>

        <div
          className={cn(
            "grid w-full grid-cols-1 gap-4",
            canJoinLab ? "sm:grid-cols-3" : "sm:grid-cols-2",
          )}
        >
          <StartCard
            icon={FilePlus2}
            accent="text-sky-500"
            title="Empty Project"
            subtitle="Clean new workspace"
            onClick={() => addProject()}
          />
          <StartCard
            icon={Network}
            accent="text-amber-500"
            title="Project Template"
            subtitle="Pre-configured PKI"
            onClick={() => addProjectFromTemplate()}
          />
          {canJoinLab && (
            <StartCard
              icon={CloudCog}
              accent="text-emerald-500"
              title="Join a Lab"
              subtitle="Enter your joining code"
              onClick={() => setJoinOpen(true)}
            />
          )}
        </div>
      </div>

      <LabJoinDialog open={joinOpen} onOpenChange={setJoinOpen} />
    </div>
  )
}

function StartCard({
  icon: Icon,
  accent,
  title,
  subtitle,
  onClick,
}: {
  icon: React.ElementType
  accent: string
  title: string
  subtitle: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group flex flex-col items-start gap-3 rounded-xl border bg-card p-5 text-left",
        "shadow-sm ring-1 ring-foreground/5 transition-all outline-none",
        "hover:border-primary/40 hover:shadow-md hover:-translate-y-0.5",
        "focus-visible:ring-3 focus-visible:ring-ring/50",
      )}
    >
      <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted/60">
        <Icon className={cn("h-5 w-5", accent)} />
      </span>
      <span className="flex flex-col gap-0.5">
        <span className="text-sm font-semibold">{title}</span>
        <span className="text-xs text-muted-foreground">{subtitle}</span>
      </span>
    </button>
  )
}
