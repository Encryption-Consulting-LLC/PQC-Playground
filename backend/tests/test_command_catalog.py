"""Command-catalog parity: ``_COMMAND_CAPABILITIES`` hand-mirrors the Rust
agent's registry (pki-executor ``commands/mod.rs``). Both sides assert
against byte-identical copies of ``fixtures/command_catalog.json`` — this one
and pki-executor's ``tests/fixtures/command_catalog.json`` — so drift
fails a test on whichever side forgot, instead of surfacing as a 422 on
dispatch. Adding a command means updating BOTH fixture copies.
"""

import json
from pathlib import Path

from app.routers.executor import _COMMAND_CAPABILITIES

_FIXTURE = Path(__file__).parent / "fixtures" / "command_catalog.json"


def test_command_capabilities_match_shared_fixture() -> None:
    fixture = json.loads(_FIXTURE.read_text())
    actual = {name: cap.value for name, cap in _COMMAND_CAPABILITIES.items()}
    assert actual == fixture, (
        "_COMMAND_CAPABILITIES and tests/fixtures/command_catalog.json "
        "disagree — update the fixture here AND pki-executor's copy"
    )
