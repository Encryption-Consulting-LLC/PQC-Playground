# Pre-deployed labs and join codes

A pre-deployed lab is a PKI environment that is already built and running: real
VMs on the ESXi host, a saved topology that describes them, and nothing left for
the visitor to deploy. Visitors are given an eight-character **join code**, and
the code decides which lab they land in. One deployment of the playground can
therefore serve as many prepared labs as there are groups of people, and one
group can be pointed at a lab built specifically for it.

Redeeming a code grants exactly two things: the lab's topology, rendered on the
canvas as it was deployed, and a remote desktop session to each of its machines.
It grants nothing else. Deploying into the lab, tearing its VMs down, running
provisioning steps, editing its saved project and issuing agent commands are all
refused by the backend, not merely hidden in the interface.

## Roles in the workflow

| Step | Who | Where |
| --- | --- | --- |
| Build and deploy the lab | Operator (or guest) | Canvas app |
| Issue and revoke the join code | Admin | `/admin` → **Labs** |
| Redeem the code | Guest (or operator) | Canvas app → **Join a Lab** |
| Reclaim the lab when it is finished | Admin | `/admin` → **VM Registry** |

Issuing is deliberately admin-only: a code lets people who are not the owner see
somebody else's VMs, which is a platform decision rather than one for whichever
account happens to hold the project.

## Prerequisites

Before building a lab, confirm in `/admin`:

1. **Infrastructure → ESXi** — the shared target is configured and reachable.
2. **Infrastructure → Image / Roles** — the base image and per-role sizing are
   set and validated.
3. **Infrastructure → Guest network** — the guest IP range, prefix, gateway and
   primary DNS are set, and **IP Pool** shows enough free addresses for the
   lab's machines.
4. **Accounts** — an account to build the lab with, and one account per visitor
   who will join it. Visitors sign in with their own account and then enter the
   code; a code is not a login.

## 1. Build the lab

Sign in to the canvas app with the building account and deploy the environment
exactly as any other project:

1. Create the project. **Project Template** produces a deploy-ready two-tier PKI
   and is the usual starting point; an empty project works equally well.
2. Name the project something a visitor will recognise. The project name is what
   the admin console lists the lab under and what the visitor's tab is labelled
   with, so "AD CS Workshop — October" beats "Project 3".
3. Configure the nodes, stage the operations, and deploy. Wait until every node
   reports `deployed` and its address appears.
4. Leave the project saved. The lab needs **both** halves — running VMs *and* a
   saved project — because the code hands a joiner the topology, and VMs with no
   saved project would resolve to an empty canvas.

An operator-built lab and a guest-built lab both work. Building with a
long-lived operator account is usually preferable: the lab outlives any single
visitor, and operator VM names are not tied to a visitor's namespace.

Verify the lab is complete in `/admin` → **VM Registry** → **Environments**: the
lab appears as one environment, with every machine listed and addressed.

## 2. Issue the join code

1. Open `/admin` → **Labs**. Every deployed environment is listed with its
   owner, machine count and deployment time.
2. Find the lab and press **Issue code**. The code appears in the row, grouped
   for legibility (`ABCD-2345`); click it to copy.
3. If **Issue code** is disabled, the row's VMs have no saved project behind
   them. Confirm the project still exists under the account that deployed it,
   then reload the page.

Each lab has one live code. Codes do not expire and can be redeemed any number
of times, which is what makes a single code usable for a whole group.

## 3. Distribute it

Give visitors their account credentials and the code. Codes are drawn from an
alphabet that excludes `I`, `L`, `O`, `0` and `1`, so they survive being read
aloud, printed on a handout or pasted from a chat message; case and separators
are ignored on entry.

For a one-click route, append the code to the app URL:

```
https://<playground-host>/?join=ABCD-2345
```

The link only prefills the join dialog — the visitor still confirms — so a code
shared this way behaves the same as one typed in.

## 4. What the visitor does

1. Sign in with their own account.
2. Press **Join a Lab** on the start screen, or the key icon in the project tab
   bar if they already have projects open, and enter the code.
3. The lab opens as a tab marked `LAB`. The canvas is read-only: the palette is
   inert, nodes cannot be moved, connected or deleted, and there is nothing to
   deploy.
4. Selecting a machine shows its details in the inspector, including **Remote
   Desktop** → **Connect**, which opens a full-screen session signed in as that
   machine's administrator.

The tab survives signing out and back in — membership is recorded server-side,
so joined labs are restored on the next sign-in without re-entering the code.
Closing the tab drops this browser's copy only; the code reopens it.

## 5. Revoke, rotate, reclaim

* **Revoke** (`/admin` → **Labs** → **Revoke**) is the group-wide off switch.
  Every account that redeemed the code loses the lab and its desktops on their
  next request. The VMs are untouched.
* **Rotate** by revoking and issuing again. The new code is unrelated to the old
  one, so anyone still holding the old one stays out.
* **Reclaim** the VMs in `/admin` → **VM Registry** → **Environments** →
  **Destroy**. Revoking a code does not free ESXi capacity or pool addresses;
  only teardown does. A lab left running keeps consuming both.

## Troubleshooting

| Symptom | Cause and remedy |
| --- | --- |
| **Issue code** is disabled | No saved project backs those VMs. The project was deleted, or the VMs predate project attribution. Rebuild the lab, or recover attribution with `uv run backfill-vm-owners`. |
| Visitor sees "That doesn't look like a join code" | Fewer or more than eight characters, or a character no code contains. Re-copy the code rather than retyping it. |
| Visitor sees "That join code isn't recognised" | Well-formed but unknown — usually a code from a different deployment. |
| Visitor sees "That join code is no longer active" | The code was revoked. Issue a new one. |
| Visitor sees "This lab is no longer available" | The lab's saved project has been deleted. Revoke the code; the lab must be rebuilt. |
| No **Remote Desktop** section on a machine | The template is Linux, or the node has no VM name or address — check the machine in **VM Registry**. |
| Remote desktop reports the service is unavailable | `guacd` is not running or not reachable at `GUACD_HOST:GUACD_PORT`. It is a local sidecar; restart it. |
| Remote desktop reports no stored credential | The VM was cloned before per-VM administrator passwords existed. Nothing can set a password inside a running guest, so that machine must be redeployed. |
| Machines show a red "executor offline" dot | The agents are genuinely not connected. Diagnose in `/admin` → **VM Registry** → **Orphans**. |

## Where this lives in the code

| Concern | File |
| --- | --- |
| Code shape, membership, snapshot sanitising | `backend/src/app/core/labs.py` |
| Issuing and redeeming routes | `backend/src/app/routers/labs.py` |
| Remote-desktop reach for a member | `core/labs.enforce_own_or_joined_vm`, called by `routers/console.py` and `routers/ws.py` |
| Admin console section | `admin/src/sections/LabsSection.tsx` |
| Joining and session restore | `frontend/src/lib/labs.ts`, `frontend/src/components/canvas/LabJoinDialog.tsx` |
| Read-only canvas | `frontend/src/hooks/useJoinedLab.ts` |
