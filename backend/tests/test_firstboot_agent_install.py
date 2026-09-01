"""The agent install step must fail loudly when the agent will not run.

``$ErrorActionPreference = 'Stop'`` governs PowerShell errors only -- a native
executable's non-zero exit is not one. Without an explicit check the script runs
past a failed ``service install``, prints 'pki-executor installed', and firstboot
reports success having registered no service.

That is not a theoretical gap. Built against the MSVC dynamic CRT, the agent
exited 0xC0000135 (STATUS_DLL_NOT_FOUND) on a golden image with no VC++
redistributable, printing nothing at all. The only symptom was the deploy plan's
provision op timing out 45 minutes later with "did not phone home" -- nothing on
the guest or the backend named a missing DLL. The guard turns that into a named
firstboot error carrying the exit code.
"""

from app.core.firstboot import _AGENT_INSTALL_SCRIPTS

_WINDOWS_INSTALL_SCRIPT = _AGENT_INSTALL_SCRIPTS["windows"]
_LINUX_INSTALL_SCRIPT = _AGENT_INSTALL_SCRIPTS["linux"]

SCRIPT = _WINDOWS_INSTALL_SCRIPT.read_text(encoding="utf-8")

#: Comments are stripped before any ordering assertion: this script documents
#: the very failure it guards against, so the prose contains every phrase the
#: code does, and a naive substring search matches the explanation instead of
#: the statement.
CODE = "\n".join(
    line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#")
)


def test_the_install_step_ships_where_the_code_expects_it() -> None:
    assert _WINDOWS_INSTALL_SCRIPT.exists()
    assert _WINDOWS_INSTALL_SCRIPT.name == "40-install-executor.ps1"
    assert _LINUX_INSTALL_SCRIPT.exists()
    assert _LINUX_INSTALL_SCRIPT.name == "40-install-executor.sh"


def test_a_failed_service_install_is_not_reported_as_success() -> None:
    install_at = CODE.index("service install")
    guard_at = CODE.index("$LASTEXITCODE")
    success_at = CODE.index("Write-Output 'pki-executor installed'")

    # The check has to sit between registering the service and claiming it
    # worked, or it guards nothing.
    assert install_at < guard_at < success_at
    assert "throw" in CODE[guard_at:success_at]


def test_the_failure_is_reported_with_its_exit_code() -> None:
    # A bare "install failed" would have been no more diagnosable than the
    # timeout it replaces: 0xC0000135 is the whole answer here.
    assert "{0:X8}" in CODE


def test_the_step_still_stops_on_powershell_errors() -> None:
    assert "$ErrorActionPreference = 'Stop'" in SCRIPT


# --------------------------------------------------------------------------- #
# The Linux half. Same three properties, expressed in the shell's own terms.   #
# --------------------------------------------------------------------------- #

LINUX = _LINUX_INSTALL_SCRIPT.read_text(encoding="utf-8")
LINUX_CODE = "\n".join(
    line for line in LINUX.splitlines() if not line.lstrip().startswith("#")
)


def test_the_linux_step_aborts_on_any_failed_command() -> None:
    # `set -e` is the shell's `$ErrorActionPreference = 'Stop'`; `-u` catches
    # the unset-variable typo that would otherwise install to an empty path,
    # and `-o pipefail` keeps a failure inside a pipeline from being masked by
    # a successful tail.
    assert "set -euo pipefail" in LINUX_CODE


def test_the_linux_step_makes_the_agent_executable() -> None:
    # The v2 Linux runner copies payload files with no execute bit — only
    # *scripts* get one — so a plain copy yields a binary systemd cannot exec,
    # which presents as an agent that never phones home rather than as anything
    # about permissions.
    assert "install -D -m 0755" in LINUX_CODE
    assert "/usr/local/bin/pki-executor" in LINUX_CODE


def test_the_linux_config_is_root_only() -> None:
    # The config carries the agent's bearer token; this is the icacls
    # equivalent, and every reader of the file can impersonate the agent.
    assert "-m 0600" in LINUX_CODE
    assert "/etc/pki-executor/config.toml" in LINUX_CODE


def test_the_linux_step_enables_but_never_starts_the_service() -> None:
    # The firstboot runner owns the single reboot, which is what brings the unit
    # up — the same AutoStart contract the Windows service installer follows.
    # Starting here would race the reboot with a half-configured network.
    assert "systemctl enable pki-executor.service" in LINUX_CODE
    assert "systemctl start" not in LINUX_CODE


def test_the_linux_step_refuses_a_pre_v2_runner() -> None:
    # A pre-v2 runner ignores the manifest's `files`, so the payload was never
    # staged; failing loudly beats a confusing path error on a VM nobody can
    # reach.
    assert "FIRSTBOOT_FILES_DIR" in LINUX_CODE
    assert "exit 1" in LINUX_CODE
