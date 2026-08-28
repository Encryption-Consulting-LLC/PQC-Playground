# Install the pki-executor agent from the firstboot payload (Phase F).
#
# The v2 firstboot runner stages ISO payload `files` to a transient directory
# exported as $env:FIRSTBOOT_FILES_DIR and deletes it afterwards — so this step
# COPIES the binary + config to persistent locations and registers the Windows
# service. It does NOT start the service or reboot: the runner owns the single
# reboot, which brings the AutoStart service up.
#
# Copy + install only — no role/PKI logic. Provisioning (CA install, etc.) is
# dispatched by the backend after the agent phones home.

$ErrorActionPreference = 'Stop'

if (-not $env:FIRSTBOOT_FILES_DIR) {
    # A pre-v2 runner never sets this (it ignores the manifest's `files`), so the
    # payload was never staged. Fail loudly rather than with a confusing path error.
    throw 'FIRSTBOOT_FILES_DIR is not set — this base image predates the v2 firstboot runner; rebuild the golden image before enabling executor bundling.'
}

$stagedBinary = Join-Path $env:FIRSTBOOT_FILES_DIR 'pki-executor.exe'
$stagedConfig = Join-Path $env:FIRSTBOOT_FILES_DIR 'executor.toml'

$installDir = Join-Path $env:ProgramFiles 'PkiExecutor'
$dataDir = Join-Path $env:ProgramData 'PkiExecutor'
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$exePath = Join-Path $installDir 'pki-executor.exe'
$configPath = Join-Path $dataDir 'config.toml'   # matches config.rs default_path
Copy-Item -Path $stagedBinary -Destination $exePath -Force
Copy-Item -Path $stagedConfig -Destination $configPath -Force

# The config holds the agent's bearer token — lock it down to SYSTEM +
# Administrators (the token is also hashed-only on the backend, but defence in depth).
icacls $configPath /inheritance:r /grant 'SYSTEM:F' 'Administrators:F' | Out-Null

# Register the SCM service (AutoStart). The runner's reboot starts it; we do NOT.
& $exePath service install

# $ErrorActionPreference only governs PowerShell errors -- a native exe's exit
# code is not one, so without this the script runs on and reports success no
# matter how the agent exited. That is not hypothetical: built against the MSVC
# dynamic CRT, this binary exits 0xC0000135 (STATUS_DLL_NOT_FOUND) on an image
# with no VC++ redistributable, printing nothing at all. Firstboot then declared
# 'pki-executor installed' with no service registered, and the only symptom was
# the provision op timing out 45 minutes later on "did not phone home".
if ($LASTEXITCODE -ne 0) {
    throw ("pki-executor service install failed (exit 0x{0:X8}). " -f $LASTEXITCODE) +
          'A silent 0xC0000135 means a missing DLL -- check the agent''s imports ' +
          'against what the base image ships.'
}

Write-Output 'pki-executor installed'
