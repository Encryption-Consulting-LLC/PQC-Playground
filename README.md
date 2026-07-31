# EC PKI Playground

EC PKI Playground is a browser-based lab for designing, deploying, and
operating a Microsoft two-tier PKI on VMware ESXi. A React canvas compiles a
topology into a dependency-aware deployment plan; FastAPI and Celery clone the
machines, allocate addresses, drive the in-guest executor, stream progress,
and retain recovery and verification evidence.

## Features

- A drag-and-drop topology canvas with domain regions, typed PKI connections,
  staged changes, compiler guidance, certificate-journey and evidence views.
- Empty projects, a preconfigured PKI project template, autosave, multi-project
  tabs, and guest-to-guest project sharing.
- Local username/password authentication, optional OIDC SSO, and server-side
  operator/guest capability enforcement.
- Operator-managed ESXi, golden-image, per-role sizing, guest-network, and IP
  pool settings, including control-plane and infrastructure preflight checks.
- Canonical deployment-plan compilation, parallel Celery execution, live
  WebSocket progress, cancellation, reconciliation, teardown, and downloadable
  evidence bundles.
- Windows provisioning sequences for AD DS/DNS, domain join and leave,
  offline root and enterprise issuing CAs, certificate/CRL relay, IIS CDP/AIA,
  OCSP, certificate enrollment, verification, and convergent cleanup.
- CertSecure Manager, CBOM Secure, and CodeSign Secure Ubuntu templates.

The backend consumes `vmkit`, `configgen`, and `isokit` as versioned Git
dependencies from [VM-Setup-Scripts](https://github.com/Arnesh-EC/VM-Setup-Scripts)
rather than vendoring their source. The Windows `pki-executor.exe` is also a
separately built deployment artifact and is intentionally not committed here.

## Architecture

```text
React/Vite ──HTTP + WebSocket──> FastAPI ─────────────> MongoDB
                                   │  │
                                   │  └──pub/sub─────> Valkey/Redis
                                   │                     │
Windows guest agents ──WebSocket───┘                  Celery workers
                                                           │
                                                           └──> VMware ESXi
```

| Path | Purpose |
| --- | --- |
| `frontend/` | React 19, TypeScript, Vite, Tailwind, Zustand, TanStack Query, and XYFlow UI |
| `backend/` | FastAPI API, Celery workers, Mongo persistence, topology compiler, and sequence engine |
| `backend/agent/` | Agent configuration and optional local location for `pki-executor.exe` |
| `docs/` | Feature inventory, roadmap, and real-lab verification notes |

## Prerequisites

- Python 3.14 or newer and [`uv`](https://docs.astral.sh/uv/)
- Node.js `^20.19.0` or `>=22.12.0` and `pnpm`
- MongoDB
- Valkey or Redis (job broker, result backend, progress bus, and agent dispatch)
- For real deployments: a reachable VMware ESXi host, qualified Windows golden
  images, and an address range for cloned guests
- For guided Windows provisioning: `pki-executor.exe` and a backend URL the
  guests can reach

MongoDB is required for API startup. Valkey/Redis and the Celery workers are
required for deployment jobs, but ESXi may be configured later from the
operator settings UI.

## Local development

### 1. Start MongoDB and Valkey

Use existing services, or start disposable local containers:

```sh
docker run -d --name pki-mongo -p 27017:27017 \
  -v pki-mongo-data:/data/db mongo:8
docker run -d --name pki-valkey -p 6379:6379 valkey/valkey:8
```

The default URLs already point at these ports. All database and broker settings
can be overridden in `backend/.env`.

### 2. Configure and start the API

```sh
cd backend
cp .env.example .env
uv sync
```

Generate two independent values:

```sh
openssl rand -base64 32
openssl rand -base64 32
```

Paste them into `backend/.env` before running any backend command:

```dotenv
SESSION_SECRET=first-generated-value
SETTINGS_ENC_KEY=second-generated-value
```

`SETTINGS_ENC_KEY` must remain stable because it encrypts stored ESXi and
template secrets.

Bootstrap an operator, then start the API:

```sh
uv run create-admin operator
uv run create-admin admin --role admin
uv run start
```

The API is available at <http://127.0.0.1:8000>, its interactive documentation
at <http://127.0.0.1:8000/docs>, and its liveness endpoint at
<http://127.0.0.1:8000/api/health>.

By default a low-privilege `guest` / `guest-playground` account is seeded when
it does not already exist. Change `EXAMPLE_GUEST_USERNAME` and
`EXAMPLE_GUEST_PASSWORD`, or leave the password empty to disable seeding, for
anything beyond local development.

### 3. Start the workers

In a second terminal, using the same `backend/.env`:

```sh
cd backend
uv run worker
```

This launches two child workers:

- `esxi`: prefork workers for clone, update, and destroy operations; concurrency
  is controlled by `CLONE_CONCURRENCY` (default `3`).
- `provision`: threaded workers for agent-driven sequences; concurrency is
  controlled by `PROVISION_CONCURRENCY` (default `16`).

For separate worker hosts, use `uv run worker-esxi` and
`uv run worker-provision` instead. The API and every worker must use the same
MongoDB, broker URLs, `SESSION_SECRET`, and `SETTINGS_ENC_KEY`.

### 4. Start the frontend

```sh
cd frontend
cp .env.example .env
pnpm install
pnpm dev
```

Open <http://localhost:5432>. Vite proxies `/api` HTTP and WebSocket traffic to
`http://127.0.0.1:8000`; set `VITE_API_TARGET` to use another backend.

## Preparing real deployments

1. Sign in as an operator and open **Settings**.
2. Configure the shared ESXi target, guest subnet, datastore/network placement,
   and the four Windows role profiles.
3. Qualify each golden image and run the environment/infrastructure preflights.
   Each Windows image must keep its built-in `Administrator` account present,
   enabled, and free of "user must change password at next logon" — first boot
   resets that account's password with `Set-LocalUser`, which neither creates the
   account nor clears that flag. A renamed, deleted, or must-change builtin makes
   first boot fail, and a local password policy stricter than the console's own
   (12 characters, 3 character classes) makes the reset throw.
4. Place the agent at `backend/agent/pki-executor.exe` or set
   `EXECUTOR_AGENT_PATH` to a path readable by both API and worker hosts.
5. Set `BACKEND_PUBLIC_URL` to the origin deployed guests use to reach the API.
   The agent connects at `/api/executor/connect`; the URL must therefore be
   reachable from the ESXi guest network and support WebSocket upgrades.
6. Set the domain controller node's **domain admin password** to something you
   control. It is the lab's single Windows credential: the DC's first-boot ISO
   resets the guest's built-in local `Administrator` to it, `Install-ADDSForest`
   promotes that account into the domain, and every later domain join plus the
   issuing-CA install then sign in as `<NETBIOS>\Administrator` with it.
   Deploying a domain controller without one is rejected with a 422.
7. Create the preconfigured PKI project, review the compiler output, and deploy.

Environment values such as `ESXI_*`, `CLONE_*`, and `GUEST_*` only seed the
Mongo settings document when values are absent. Once seeded, the operator-edited
document is authoritative and changes take effect without an API restart. See
[`backend/.env.example`](backend/.env.example) for the complete configuration
reference, including OIDC and agent timing controls.

## Authentication and persistence

Every user signs in; there is no anonymous mode. Requests use the backend-issued
session token in `X-Session-Token`, while WebSockets pass it as a query
parameter. Account disablement and role changes are checked on every request.

- Operators have the complete capability set, manage users and shared
  infrastructure settings, and store projects in MongoDB.
- Guests receive the guided VM/deploy subset, store ordinary projects in
  browser local storage, and can explicitly publish or accept opaque project
  share links. Guest VM names are server-namespaced and destructive operations
  are ownership-checked.

OIDC is enabled only when all required `OIDC_*` values are present. IdP groups
map to operator or guest roles using the configured exact-match group lists.

## API surface

All application routes are mounted below `/api`.

| Area | Routes |
| --- | --- |
| Health and scripts | `/api/health`, `/api/generate/*` |
| Sessions and users | `/api/auth/*`, `/api/admin/users` |
| Projects and sharing | `/api/projects`, `/api/project-shares/*` |
| Settings and preflight | `/api/settings`, `/api/settings/*/validate`, `/api/ip-pool` |
| Direct VM operations | `/api/vm/*`, `/api/vm-registry` |
| Plans and recovery | `/api/deploy/compile`, `/api/deploy`, reconcile, teardown, cancel, and evidence routes |
| ISO authoring | `/api/iso/*` |
| Agents and progress | `/api/executor/*`, `/api/ws/jobs/{job_id}` |

FastAPI's generated OpenAPI document is the source of truth for request and
response schemas. Except for liveness, login/OIDC entry points, and agent
registration/connect flows, routes require an authenticated capability.

## Verification

With `backend/.env` configured:

```sh
cd backend
uv run ruff check .          # lint
uv run ruff format --check . # formatting (drop --check to apply)
uv run pytest
```

Frontend checks:

```sh
cd frontend
pnpm exec biome format .     # formatting (add --write to apply)
pnpm test
pnpm lint
pnpm build
```

### Pre-commit hook

A version-controlled hook in [`.githooks/pre-commit`](.githooks/pre-commit)
runs these checks automatically before each commit, scoped to the package that
has staged changes (a docs-only or single-side commit stays fast):

- `backend/` staged → `ruff check .` (lint), `ruff format --check` on staged
  `.py`, then `uv run pytest -q`
- `frontend/` staged → `biome format` on staged files, then `pnpm exec tsc -b`,
  `pnpm test`, `pnpm lint`

Formatting is **check-only** — the hook never rewrites your files, it only
errors on unformatted code (fix with `ruff format` / `biome format --write`).
Format checks are scoped to *staged* files, so the not-yet-formatted legacy
files don't block unrelated commits; you format each file as you touch it.

Hooks are not copied on clone, so enable it once per checkout:

```sh
git config core.hooksPath .githooks
```

Bypass with `git commit --no-verify` when needed.

These checks do not replace the ESXi/Windows canary. Before treating a guided
PKI deployment as successful, follow
[`docs/two-tier-lab-verification.md`](docs/two-tier-lab-verification.md) and
confirm the final structured health result, OCSP/CDP/AIA retrieval, enterprise
PKI state, and retained evidence bundle on real hardware.

## Further reading

- [Full two-tier PKI roadmap](docs/full-two-tier-pki-roadmap.md)
- [Two-tier lab verification notes](docs/two-tier-lab-verification.md)
