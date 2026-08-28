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

from app.core.firstboot import _AGENT_INSTALL_SCRIPT

SCRIPT = _AGENT_INSTALL_SCRIPT.read_text(encoding="utf-8")

#: Comments are stripped before any ordering assertion: this script documents
#: the very failure it guards against, so the prose contains every phrase the
#: code does, and a naive substring search matches the explanation instead of
#: the statement.
CODE = "\n".join(
    line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#")
)


def test_the_install_step_ships_where_the_code_expects_it() -> None:
    assert _AGENT_INSTALL_SCRIPT.exists()
    assert _AGENT_INSTALL_SCRIPT.name == "40-install-executor.ps1"


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
