"""Celery application for the deploy job queues.

Separate worker processes run the tasks in ``app.tasks``; the FastAPI process
only ever calls ``.delay(...)``/``.apply_async(...)`` on them. Work is split
across two queues with independent concurrency (``uv run worker`` launches
both — see ``app.cli.worker``):

- ``esxi`` — every task that opens an ESXi connection (clones, destroys, plan
  preflight). Its worker's ``--concurrency`` (``Settings.clone_concurrency``)
  is the global cap on simultaneous clones against the shared ESXi host.
- ``provision`` — everything else (post-clone provisioning, sequence ops,
  reconciles). These mostly sleep on Valkey pub/sub waiting for agents, so the
  cap (``Settings.provision_concurrency``) is much higher; the worker also
  drains the legacy default ``celery`` queue so in-flight pre-split jobs
  survive an upgrade.

``run_plan_operation_v2`` is queue-routed per op by ``tasks._advance_plan``
(createVm → esxi, everything else → provision) — the one decision
``task_routes`` can't express.
"""

from celery import Celery

from app.core.settings import settings

celery_app = Celery(
    "pki_deploy",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Re-deliver a task if the worker dies mid-clone rather than silently dropping
    # it; the retry surfaces as VmExistsError -> a 409 ErrorMsg if it partially
    # completed, which is preferable to losing the job outright.
    task_acks_late=True,
    # With --concurrency=N this makes N the *true* ceiling on in-flight clones —
    # without it, a worker can prefetch and hold extra tasks past the cap.
    worker_prefetch_multiplier=1,
    # We stream all state over the Valkey pub/sub + snapshot transport, not by
    # polling AsyncResult, so just bound how long results linger.
    result_expires=3600,
    # Everything lands on the provision queue unless routed otherwise below
    # (or per-call, as _advance_plan does for plan operations). Nothing may
    # ever land on a queue no worker consumes.
    task_default_queue="provision",
    task_routes={
        "clone_vm": {"queue": "esxi"},
        "destroy_vm": {"queue": "esxi"},
        "admin_teardown": {"queue": "esxi"},
        "start_plan_v2": {"queue": "esxi"},
        "teardown_plan": {"queue": "esxi"},
    },
)

# Import so the task is registered with this app instance.
import app.tasks  # noqa: E402, F401
