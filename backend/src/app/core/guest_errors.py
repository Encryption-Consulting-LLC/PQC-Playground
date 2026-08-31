"""What a guest is told when something fails.

The rest of the backend writes error text for whoever has to *diagnose* the
platform — datastore names and free bytes, projected usage against the admin's
own limit, VMX paths, image revisions, port groups, agent digests, the words
MongoDB / Valkey / Celery / ESXi, and on an unexpected worker failure a Python
traceback. An operator needs every word of that. A guest evaluating a product
can act on none of it, and some of it is somebody else's infrastructure.

This module is the one screen where the whole guest voice can be read, and the
narrowing happens server-side (``core/errors`` for HTTP, ``routers/ws`` for the
job stream) so the internals never reach a guest's browser at all. Mongo and the
Valkey snapshot keep the raw text, so the admin console's Deployments view and
``core/jobs/replay`` are unaffected — the narrowing belongs at the boundary that
knows who is reading, not at the producer.

The register is deliberate: formal, no contractions, one sentence, and no
remediation advice, because almost nothing a guest hits here is theirs to fix.

Two things it is careful *not* to flatten:

* messages already written for a guest — the join-code refusals, the console
  refusals, the joined-lab 403. They are the existing house style and some of
  them carry the one action that works (a VM with no stored credential really
  does need a redeploy, and must not read as a transient failure). They are
  entries in the same table whose guest sentence *is* the original, which costs
  a line each and keeps the whole voice reviewable in one place.
* topology compilation diagnostics, which name the guest's own nodes ("Issuing
  CA has no root CA parent.") and are the canvas's only guidance on an invalid
  drawing. Vaguing those would make the canvas unusable, which is the opposite
  of friendlier.

The important property is that it is **fail-closed**: the status code alone
picks a safe vague sentence, and the marker table only ever *upgrades* a
recognised class to something more specific. If a marker stops matching — a
message is reworded upstream, a vmkit release changes its text — the guest falls
back to the vague default. A marker can never cause a leak, only miss an
improvement.

Pure and stdlib-only, so the Celery worker and the tests can import it without
the FastAPI or Mongo stack.
"""

#: The catch-all when nothing more specific is known. Chosen by status code, so
#: an unrecognised failure is narrowed rather than passed through.
_GENERIC = "Something went wrong. Please try again."

_BY_STATUS: dict[int, str] = {
    400: "That request could not be processed.",
    401: "Your session is no longer valid.",
    403: "Your account is not permitted to do that.",
    404: "That item was not found.",
    409: "That action conflicts with the current state of this lab.",
    410: "That is no longer available.",
    422: "This topology cannot be deployed as it is drawn.",
    500: "Something went wrong while deploying this lab.",
    502: "The lab environment is temporarily unavailable.",
    503: "The lab environment is temporarily unavailable.",
    504: "The lab environment is temporarily unavailable.",
}

#: ``(marker, guest sentence)``, most specific first — the first marker found in
#: the raw detail wins. Markers are matched case-insensitively as substrings, so
#: they survive the surrounding f-string context (a preflight detail arrives
#: wrapped in "Infrastructure preflight no longer passes: ...", an op error
#: arrives bare). Status is deliberately *not* part of the key: the same failure
#: reaches a guest as a 409 over HTTP and as a bare op detail over the socket.
#:
#: Entries whose sentence equals the original are messages already written for a
#: guest — listed here so this file is the whole guest voice, not most of it.
_MARKERS: tuple[tuple[str, str], ...] = (
    # --- already guest-facing: kept verbatim -------------------------------
    ("join code isn't recognised", "That join code isn't recognised."),
    ("join code is no longer active", "That join code is no longer active."),
    (
        "look like a join code",
        "That doesn't look like a join code. Codes are 8 characters.",
    ),
    (
        "lab is no longer available",
        "This lab is no longer available. Ask for a new join code.",
    ),
    (
        "still being created",
        "This VM is still being created — try again once it is ready.",
    ),
    (
        "no address yet",
        "This VM has no address yet, so there is nothing to connect to.",
    ),
    (
        "no stored credentials",
        "No stored credentials for this VM. Remote desktop needs a password set "
        "at first boot, so redeploy this node to enable it.",
    ),
    (
        "deployed for you to explore",
        "This lab was deployed for you to explore. Create your own project to "
        "build and deploy a topology.",
    ),
    # --- narrowed ----------------------------------------------------------
    # Capacity. The example that started this: a guest was shown
    # "Reserved 68719476736 bytes; projected usage 92.31% (limit 85.00%)."
    ("projected usage", "There is not enough space to deploy this lab."),
    ("insufficient space", "There is not enough space to deploy this lab."),
    ("datastore capacity", "There is not enough space to deploy this lab."),
    ("project datastore usage", "There is not enough space to deploy this lab."),
    # The pool is finite and shared; "widen the range in settings" was advice
    # for somebody who is not the reader.
    (
        "ip pool exhausted",
        "This environment has no free addresses for a new lab.",
    ),
    # Preflight, both the route's 409 and the worker's re-check, which
    # concatenates every failed check into one line.
    (
        "preflight",
        "This environment is not ready to deploy a lab right now.",
    ),
    (
        "prerequisites changed",
        "This environment is not ready to deploy a lab right now.",
    ),
    # Named infrastructure. A guest cannot configure any of it.
    ("esxi", "The lab environment is temporarily unavailable."),
    ("executor_agent_path", "The lab environment is temporarily unavailable."),
    ("settings_enc_key", "The lab environment is temporarily unavailable."),
    ("guest ip range", "The lab environment is temporarily unavailable."),
    # Cancellation is not a failure and reads badly as one.
    ("cancellation requested", "The deployment was cancelled."),
    ("cancellation was requested", "The deployment was cancelled."),
    # A machine that never came up, kept apart from one that came up and then
    # failed to configure — it is the difference between waiting and retrying.
    ("did not phone home", "This machine did not finish starting up."),
    ("did not reconnect", "This machine did not finish starting up."),
    ("boot did not settle", "This machine did not finish starting up."),
    ("still pending after", "This machine did not finish starting up."),
    ("no live agent", "This machine is not reachable right now."),
    ("no executor agent", "This machine is not reachable right now."),
    ("no live vm registered", "This machine is not reachable right now."),
    ("disconnected while running", "This machine is not reachable right now."),
    # The terminal PKI gate. Distinct because the lab exists and mostly works.
    ("health gate", "The lab did not pass its final health check."),
    # Everything the sequence engine and the agent bus raise: step ids, artifact
    # keys, probe command names, verify windows, raw PowerShell stderr.
    ("step '", "This machine could not be set up."),
    ("agent command", "This machine could not be set up."),
    ("timed out after", "This machine could not be set up."),
    ("provisioning failed", "This machine could not be set up."),
    # Naming collisions are the guest's own, but the derived name spells out the
    # internal guest-<user>-<project>-<machine> scheme.
    ("already exist", "A machine with that name already exists in this environment."),
    # Entitlement, kept distinct from a plain permission refusal — the account
    # is fine, the product is simply not part of its catalogue.
    ("is not available to account", "This product is not available to your account."),
    ("does not have capability", "Your account is not permitted to do that."),
    # Remote desktop capacity: the cap is server-wide and "close one" is not
    # something a guest can do to somebody else's session.
    ("remote desktop sessions are", "Remote desktop is busy right now."),
    (
        "remote desktop service is unavailable",
        "The remote desktop service is unavailable.",
    ),
)


def guest_sentence(status: int, detail: object) -> str:
    """The one sentence a guest is shown for *detail* raised with *status*.

    Fail-closed: an unrecognised detail returns the status default, never the
    original text.
    """

    if isinstance(detail, str):
        haystack = detail.casefold()
        for marker, sentence in _MARKERS:
            if marker in haystack:
                return sentence
    return _BY_STATUS.get(status, _GENERIC)


def guest_http_detail(status: int, detail: object) -> object:
    """Narrow a FastAPI ``{"detail": ...}`` payload for a guest.

    Structured details keep only what a guest can use. ``diagnostics`` (topology
    compilation) is the documented pass-through — it names the guest's own nodes
    and is the canvas's only guidance on an invalid drawing. ``preflight`` is
    dropped outright: every row of it names a datastore, VMX path, image revision
    or port group, even the rows that passed.
    """

    if isinstance(detail, dict):
        diagnostics = detail.get("diagnostics")
        if diagnostics is not None:
            return {
                "message": "This topology cannot be deployed as it is drawn.",
                "diagnostics": diagnostics,
            }
        return guest_sentence(status, detail.get("message"))
    return guest_sentence(status, detail)


def _guest_op_state(state: object) -> object:
    """One ``OpRunState`` as a guest sees it.

    ``trace`` is dropped rather than narrowed — a Python traceback has no guest
    audience at all. ``result`` is left alone: it carries the lab health report
    and the certificate journey, which are guest-facing *features* the canvas
    renders (``store/staging.ts`` reads ``result.health``), and its nested
    ``detail`` keys are structured diagnostics rather than prose. That is also
    why this rewrites named fields instead of walking for every key called
    "detail" — such a walker would quietly corrupt the health report.
    """

    if not isinstance(state, dict):
        return state
    narrowed = {key: value for key, value in state.items() if key != "trace"}
    if isinstance(narrowed.get("detail"), str):
        narrowed["detail"] = guest_sentence(500, narrowed["detail"])
    steps = narrowed.get("steps")
    if isinstance(steps, dict):
        narrowed["steps"] = {
            step_id: _guest_op_state(step) for step_id, step in steps.items()
        }
    return narrowed


def _guest_ops(ops: object) -> object:
    if not isinstance(ops, dict):
        return ops
    return {op_id: _guest_op_state(state) for op_id, state in ops.items()}


def guest_job_payload(payload: dict) -> dict:
    """Narrow one outbound job-stream frame for a guest.

    Handles the three shapes that carry failure text. ``PlanStateMsg`` keeps its
    ops at the top level; the terminal ``DoneMsg`` keeps them at ``result.ops``,
    which is the frame a failed deploy actually ends on — narrowing only the
    top-level key would leave every real failure reaching the guest verbatim.
    Anything else in ``result`` is passed through: an ad-hoc executor command
    publishes its own output there, and that output is the guest's own.
    """

    if not isinstance(payload, dict):
        return payload

    narrowed = dict(payload)
    if isinstance(narrowed.get("detail"), str):
        narrowed["detail"] = guest_sentence(
            narrowed.get("status") if isinstance(narrowed.get("status"), int) else 500,
            narrowed["detail"],
        )
    if "ops" in narrowed:
        narrowed["ops"] = _guest_ops(narrowed["ops"])
    result = narrowed.get("result")
    if isinstance(result, dict) and "ops" in result:
        narrowed["result"] = {**result, "ops": _guest_ops(result["ops"])}
    return narrowed
