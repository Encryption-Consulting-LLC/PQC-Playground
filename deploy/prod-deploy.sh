#!/usr/bin/env bash
#
# prod-deploy.sh — production deploy for EC PKI Playground on a single Linux host.
#
# What it does (idempotent — safe to re-run for upgrades):
#   1. Update the app repo in place (the checkout this script lives in).
#   2. Ensure backend/.env and deploy/.env exist — prompting once for any
#      site-specific value still missing, then persisting it so re-runs are
#      non-interactive.
#   3. Install backend deps (uv sync) and download the Windows executor
#      agent (wget from GitHub Releases) into backend/agent/.
#   4. Build the frontend and admin app (pnpm build → */dist), both served
#      same-origin by the API's static mounts.
#   5. Health-check the already-running MongoDB and Valkey.
#   6. Seed the first admin account on the FIRST deploy only (prompts for
#      credentials interactively, or auto-generates a password unattended).
#      Redeploys detect the existing admin and skip this entirely — no prompt.
#   7. Install and (re)start systemd *user* services: API and both Celery
#      workers. Enable linger so they start at boot.
#
# The executor binary is a Windows artifact — it is NOT run here; it is
# fetched so the worker can bundle it into firstboot ISOs.
#
# This script bakes in no site-specific values, so it is safe to publish. Site
# config is prompted on the first run and persisted (deploy-time answers in
# deploy/.env, backend runtime config in backend/.env — both git-ignored); later
# runs read those files and prompt only for whatever is still missing. Any value
# can be forced non-interactively by exporting it inline, e.g.:
#   BACKEND_PUBLIC_URL=https://pki.example.com \
#   EXEC_RELEASE_REPO=example-org/pki-executor ./deploy/prod-deploy.sh
#
set -euo pipefail

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
# This script ships inside the repo, so the checkout it lives in is the default
# deploy target — no re-clone needed. Override APP_DIR only to bootstrap a fresh
# checkout somewhere else (then the clone fallback in step 1 kicks in).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="${APP_DIR:-$REPO_ROOT}"
BRANCH="${BRANCH:-master}"

# Persisted deploy-time answers (git-ignored by the repo's `.env` rule). Holds
# the site-specific, sometimes-secret values this script no longer hardcodes:
# REPO_URL, EXEC_RELEASE_REPO, GITHUB_TOKEN. Created 0600 (may hold a token).
DEPLOY_ENV="$SCRIPT_DIR/.env"
touch "$DEPLOY_ENV" && chmod 600 "$DEPLOY_ENV"

# Non-sensitive knobs keep plain defaults (localhost / conventional names);
# override any of them inline. Site-specific values are resolved via ensure_var
# below (prompt-once, persist) rather than hardcoded here.
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
EXEC_RELEASE_TAG="${EXEC_RELEASE_TAG:-latest}"   # git tag, or "latest"
EXEC_ASSET="${EXEC_ASSET:-pki-executor.exe}"     # asset filename on the release

# Datastores are assumed already running; we only health-check them.
MONGO_URL="${MONGO_URL:-mongodb://localhost:27017}"
VALKEY_URL="${VALKEY_URL:-redis://localhost:6379/0}"

SYSTEMD_DIR="$HOME/.config/systemd/user"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[err]\033[0m %s\n' "$*" >&2; exit 1; }

require_tool() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

# Resolve a config value and persist it, so re-runs are non-interactive.
#   ensure_var FILE KEY PROMPT [DEFAULT] [FLAGS]
# Resolution order: inline env override > value already in FILE > interactive
# prompt (default in brackets) > DEFAULT. The resolved value is exported for the
# rest of the run and appended to FILE if not already present (never clobbered).
# FLAGS (space-separated, any order): `secret` hides the input; `optional`
# allows an empty result (nothing persisted) instead of aborting.
ensure_var() {
  local file="$1" key="$2" prompt="$3" default="${4:-}" flags="${5:-}"
  local hide=0 opt=0 val="${!key:-}"
  [[ "$flags" == *secret* ]]   && hide=1
  [[ "$flags" == *optional* ]] && opt=1
  if [ -z "$val" ] && [ -f "$file" ]; then
    # `|| true`: a no-match grep returns 1, which under `set -o pipefail`
    # would otherwise abort the whole script via `set -e`.
    val="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -1 | sed 's/^[[:space:]]*[^=]*=//' || true)"
  fi
  if [ -z "$val" ]; then
    if [ -t 0 ]; then
      if [ "$hide" -eq 1 ]; then read -rs -p "$prompt: " val; echo
      else read -r -p "$prompt${default:+ [$default]}: " val; fi
    fi
    val="${val:-$default}"
  fi
  if [ -z "$val" ]; then
    [ "$opt" -eq 1 ] && { printf -v "$key" '%s' ''; export "$key"; return 0; }
    die "$key is required — provide it interactively or export it inline."
  fi
  printf -v "$key" '%s' "$val"; export "$key"
  grep -Eq "^[[:space:]]*${key}=" "$file" 2>/dev/null || {
    printf '%s=%s\n' "$key" "$val" >>"$file"
    [ "$hide" -eq 1 ] && log "Saved $key to ${file##*/} (hidden)" \
                      || log "Saved $key to ${file##*/}"
  }
}

# Extract host:port from a mongodb:// or redis:// URL and test TCP reachability.
check_tcp() {
  local name="$1" url="$2" hostport host port
  hostport="${url#*://}"       # strip scheme
  hostport="${hostport%%/*}"   # strip /path and /db
  hostport="${hostport##*@}"   # strip user:pass@
  host="${hostport%%:*}"
  port="${hostport##*:}"
  [ "$host" = "$port" ] && port=""   # no ':' present
  [ -z "$port" ] && case "$url" in redis:*) port=6379;; *) port=27017;; esac
  if timeout 3 bash -c ": >/dev/tcp/$host/$port" 2>/dev/null; then
    log "$name reachable at $host:$port"
  else
    die "$name not reachable at $host:$port — start it before deploying."
  fi
}

# ----------------------------------------------------------------------------
# 0. Preflight
# ----------------------------------------------------------------------------
log "Preflight: checking tools"
for t in git uv pnpm node wget openssl systemctl loginctl timeout; do
  require_tool "$t"
done

# Resolve deploy-time config (persisted to deploy/.env; prompted once). REPO_URL
# defaults to this checkout's own origin remote, so the common in-place upgrade
# needs no input. EXEC_RELEASE_REPO/GITHUB_TOKEN gate the agent download below.
ensure_var "$DEPLOY_ENV" REPO_URL \
  "Git repo URL to deploy from" \
  "$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
ensure_var "$DEPLOY_ENV" EXEC_RELEASE_REPO \
  "Executor agent release repo (owner/repo, blank to skip agent download)" \
  "" optional
ensure_var "$DEPLOY_ENV" GITHUB_TOKEN \
  "GitHub token for private release assets (blank for a public repo)" \
  "" "secret optional"

# ----------------------------------------------------------------------------
# 1. Update the repo (in place by default — see APP_DIR above; clone only when
#    APP_DIR points somewhere that isn't a checkout yet)
#
#    Push-to-deploy: when invoked from the post-receive hook (deploy/hooks/
#    post-receive), the push has already updated the working tree via
#    receive.denyCurrentBranch=updateInstead, so re-pulling from origin here
#    would fight the direct push. The hook sets DEPLOY_SKIP_GIT_UPDATE=1.
# ----------------------------------------------------------------------------
if [ "${DEPLOY_SKIP_GIT_UPDATE:-0}" = "1" ]; then
  log "Skipping repo update — push-to-deploy already updated the working tree"
elif [ -d "$APP_DIR/.git" ]; then
  log "Updating existing checkout at $APP_DIR"
  git -C "$APP_DIR" fetch --prune origin
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  log "Cloning $REPO_URL -> $APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

BACKEND="$APP_DIR/backend"
FRONTEND="$APP_DIR/frontend"
ADMIN="$APP_DIR/admin"
AGENT_DIR="$BACKEND/agent"

# ----------------------------------------------------------------------------
# 2. Ensure backend/.env — generated secrets + prompted-once site config
# ----------------------------------------------------------------------------
ENV_FILE="$BACKEND/.env"
if [ ! -f "$ENV_FILE" ]; then
  log "Creating $ENV_FILE from .env.example"
  cp "$BACKEND/.env.example" "$ENV_FILE"
fi

# Generate a key only if it is not already present uncommented (never clobber:
# rotating SETTINGS_ENC_KEY would orphan every stored ESXi/template secret).
ensure_generated() {
  local key="$1" value="$2"
  grep -Eq "^[[:space:]]*${key}=" "$ENV_FILE" && return 0
  printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  log "Generated $key in .env"
}
ensure_generated SESSION_SECRET "$(openssl rand -base64 32)"
ensure_generated SETTINGS_ENC_KEY "$(openssl rand -base64 32)"

# Site config: prompted once, then read from backend/.env on every re-run.
ensure_var "$ENV_FILE" BACKEND_PUBLIC_URL \
  "Backend public URL browsers and agents dial (e.g. https://pki.example.com)"
ensure_var "$ENV_FILE" AGENT_BACKEND_URL \
  "Agent phone-home URL override (blank = use BACKEND_PUBLIC_URL)" "" optional

grep -Eq '^[[:space:]]*ESXI_HOST=' "$ENV_FILE" || \
  warn "ESXI_* / GUEST_* not set in .env — configure them here or via the operator Settings UI before deploying VMs."

# ----------------------------------------------------------------------------
# 3. Backend deps + executor agent
# ----------------------------------------------------------------------------
log "Installing backend deps (uv sync)"
( cd "$BACKEND" && uv sync )

mkdir -p "$AGENT_DIR"
if [ -z "${EXEC_RELEASE_REPO:-}" ]; then
  if [ -f "$AGENT_DIR/pki-executor.exe" ]; then
    warn "EXEC_RELEASE_REPO unset — skipping agent download; keeping the existing pki-executor.exe."
  else
    warn "EXEC_RELEASE_REPO unset and no bundled agent present — firstboot ISOs will be agent-free."
  fi
else
  log "Downloading executor agent ($EXEC_RELEASE_REPO@$EXEC_RELEASE_TAG / $EXEC_ASSET)"
  if [ "$EXEC_RELEASE_TAG" = "latest" ]; then
    EXEC_URL="https://github.com/$EXEC_RELEASE_REPO/releases/latest/download/$EXEC_ASSET"
  else
    EXEC_URL="https://github.com/$EXEC_RELEASE_REPO/releases/download/$EXEC_RELEASE_TAG/$EXEC_ASSET"
  fi
  WGET_ARGS=(--quiet --show-progress)
  [ -n "${GITHUB_TOKEN:-}" ] && WGET_ARGS+=(--header="Authorization: Bearer $GITHUB_TOKEN")
  TMP_AGENT="$(mktemp)"
  if wget "${WGET_ARGS[@]}" -O "$TMP_AGENT" "$EXEC_URL" && [ -s "$TMP_AGENT" ]; then
    mv "$TMP_AGENT" "$AGENT_DIR/pki-executor.exe"
    log "Agent updated ($(du -h "$AGENT_DIR/pki-executor.exe" | cut -f1))"
  else
    rm -f "$TMP_AGENT"
    if [ -f "$AGENT_DIR/pki-executor.exe" ]; then
      warn "Agent download failed ($EXEC_URL) — keeping the existing pki-executor.exe."
    else
      die "Agent download failed ($EXEC_URL) and no existing binary present. Fix EXEC_RELEASE_* (and GITHUB_TOKEN if private)."
    fi
  fi
fi

# ----------------------------------------------------------------------------
# 4. Build the frontend and admin app (both served same-origin by the API's
#    static mounts — see app/main.py::_mount_frontend / _mount_admin)
# ----------------------------------------------------------------------------
log "Building frontend"
( cd "$FRONTEND" && pnpm install --frozen-lockfile && pnpm build )
[ -f "$FRONTEND/dist/index.html" ] || die "frontend build produced no dist/index.html"

log "Building admin app"
( cd "$ADMIN" && pnpm install --frozen-lockfile && pnpm build )
[ -f "$ADMIN/dist/index.html" ] || die "admin build produced no dist/index.html"

# ----------------------------------------------------------------------------
# 5. Health-check datastores (assumed already running)
# ----------------------------------------------------------------------------
log "Health-checking datastores"
check_tcp MongoDB "$MONGO_URL"
check_tcp Valkey "$VALKEY_URL"

# ----------------------------------------------------------------------------
# 6. Seed the first admin account — first deploy only
#
#    Admin is a separate role from operator (core/authz.py) — it manages the
#    ESXi target, base images, and accounts via the /admin console, and has
#    no access to the operator canvas.
#
#    Redeploys run unattended: `admin-exists` (backend/src/app/cli.py) reports
#    whether *any* admin account is already present, and if so this whole block
#    is skipped — no username/password prompt on every upgrade. Only a truly
#    un-bootstrapped install (no admin at all) provisions one, either from
#    ADMIN_USERNAME/ADMIN_PASSWORD, an interactive prompt, or an auto-generated
#    password. Set FORCE_ADMIN_PROVISION=1 to provision even when an admin
#    exists (e.g. to add another admin non-interactively via ADMIN_USERNAME).
# ----------------------------------------------------------------------------
ADMIN_NOTE=""
if [ "${FORCE_ADMIN_PROVISION:-0}" != "1" ] && ( cd "$BACKEND" && uv run admin-exists ) 2>/dev/null; then
  log "Admin account already provisioned — skipping bootstrap (redeploy)."
else
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
  ADMIN_PASSWORD_GENERATED=0

  if [ -z "$ADMIN_PASSWORD" ] && [ -t 0 ]; then
    log "Provisioning the admin account (press Enter on the password prompt to auto-generate one)"
    read -r -p "Admin username [$ADMIN_USERNAME]: " admin_username_input
    ADMIN_USERNAME="${admin_username_input:-$ADMIN_USERNAME}"
    read -rs -p "Admin password (blank to auto-generate): " admin_password_input
    echo
    if [ -n "$admin_password_input" ]; then
      read -rs -p "Repeat password: " admin_password_confirm
      echo
      [ "$admin_password_input" = "$admin_password_confirm" ] || die "Passwords did not match."
      ADMIN_PASSWORD="$admin_password_input"
    fi
  fi
  if [ -z "$ADMIN_PASSWORD" ]; then
    ADMIN_PASSWORD="$(openssl rand -base64 18)"
    ADMIN_PASSWORD_GENERATED=1
  fi

  log "Provisioning admin account '$ADMIN_USERNAME' (no-op if it already exists)"
  CREATE_ADMIN_OUTPUT="$(cd "$BACKEND" && ADMIN_PASSWORD="$ADMIN_PASSWORD" uv run create-admin "$ADMIN_USERNAME" --role admin)"
  printf '%s\n' "$CREATE_ADMIN_OUTPUT"

  # Only surface the generated password if the account was actually just
  # created — if the chosen username happened to already exist, create-admin
  # no-ops and the freshly-generated string above was never applied, so
  # printing it would show a password that isn't the real one.
  if [ "$ADMIN_PASSWORD_GENERATED" -eq 1 ] && printf '%s' "$CREATE_ADMIN_OUTPUT" | grep -qF "Created admin account '$ADMIN_USERNAME'."; then
    ADMIN_NOTE="  - Generated admin credentials (shown once — store them securely, then rotate via the
    admin console or \`uv run create-admin $ADMIN_USERNAME --role admin\` under a fresh password):
      username: $ADMIN_USERNAME
      password: $ADMIN_PASSWORD
"
  fi
fi

# ----------------------------------------------------------------------------
# 7. systemd user services
# ----------------------------------------------------------------------------
log "Installing systemd user units into $SYSTEMD_DIR"
mkdir -p "$SYSTEMD_DIR"
UV_BIN="$(command -v uv)"

# API — single uvicorn worker on purpose: the agent-dispatch bridge forwards to
# whichever process holds the agent WebSocket, so multiple workers would break
# worker→agent dispatch. Reload is off (that's dev-only via `uv run start`).
cat >"$SYSTEMD_DIR/pki-api.service" <<EOF
[Unit]
Description=EC PKI Playground — API (uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BACKEND
ExecStart=$UV_BIN run uvicorn app.main:app --host $API_HOST --port $API_PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=pki.target default.target
EOF

cat >"$SYSTEMD_DIR/pki-worker-esxi.service" <<EOF
[Unit]
Description=EC PKI Playground — Celery worker (esxi queue)
After=network-online.target pki-api.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BACKEND
ExecStart=$UV_BIN run worker-esxi
Restart=always
RestartSec=5

[Install]
WantedBy=pki.target default.target
EOF

cat >"$SYSTEMD_DIR/pki-worker-provision.service" <<EOF
[Unit]
Description=EC PKI Playground — Celery worker (provision queue)
After=network-online.target pki-api.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BACKEND
ExecStart=$UV_BIN run worker-provision
Restart=always
RestartSec=5

[Install]
WantedBy=pki.target default.target
EOF

cat >"$SYSTEMD_DIR/pki.target" <<EOF
[Unit]
Description=EC PKI Playground — full stack
Wants=pki-api.service pki-worker-esxi.service pki-worker-provision.service

[Install]
WantedBy=default.target
EOF

log "Enabling boot start (linger)"
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || sudo loginctl enable-linger "$USER" \
    || warn "Could not enable linger — services won't start until you next log in. Run: sudo loginctl enable-linger $USER"
fi

log "Reloading and (re)starting services"
SERVICES=(pki-api.service pki-worker-esxi.service pki-worker-provision.service)
systemctl --user daemon-reload
systemctl --user enable pki.target "${SERVICES[@]}"
systemctl --user restart "${SERVICES[@]}"

# ----------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------
log "Deploy complete. Status:"
systemctl --user --no-pager --no-legend status \
  pki-api.service pki-worker-esxi.service pki-worker-provision.service \
  | sed -n '1,4p;/Active:/p' || true

cat <<EOF

Next steps:
  - Admin console: $API_HOST:$API_PORT/admin
${ADMIN_NOTE}  - Bootstrap an operator or guest (interactive password prompt):
      cd $BACKEND && uv run create-admin <name> --role operator
  - Logs:   journalctl --user -u pki-api -f   (or -worker-esxi / -worker-provision)
  - Control: systemctl --user restart pki.target   |   systemctl --user stop pki.target
  - API listening at: $API_HOST:$API_PORT  (SPA + /api + /admin same origin)
EOF
