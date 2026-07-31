import argparse
import getpass
import multiprocessing
import os
import sys
import time

import uvicorn


def main() -> None:
    """Dev server. Binds all interfaces (``API_HOST``/``API_PORT`` to override):
    guest VMs phone home to the backend directly over the LAN, so a
    loopback-only listener makes every agent connection fail against a dev
    backend too."""
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", "8000")),
        reload=True,
    )


def _esxi_worker_argv() -> list[str]:
    """Heavy blocking pyVmomi/isokit work — prefork, capped at the global
    simultaneous-clone ceiling for the shared ESXi host."""
    from app.core.settings import settings

    return [
        "worker",
        "-E",
        "-Q",
        "esxi",
        "-n",
        "esxi@%h",
        "--pool=prefork",
        f"--concurrency={settings.clone_concurrency}",
        "--prefetch-multiplier=1",
    ]


def _provision_worker_argv() -> list[str]:
    """Provision/sequence ops mostly sleep on Valkey pub/sub — threads pool,
    high cap. Also drains the legacy default ``celery`` queue so in-flight
    pre-split jobs survive an upgrade. (If the threads pool ever misbehaves,
    ``--pool=prefork --concurrency=8`` is a safe fallback — nothing here uses
    soft_time_limit.)"""
    from app.core.settings import settings

    return [
        "worker",
        "-E",
        "-Q",
        "provision,celery",
        "-n",
        "provision@%h",
        "--pool=threads",
        f"--concurrency={settings.provision_concurrency}",
        "--prefetch-multiplier=1",
    ]


def _run_worker(argv: list[str]) -> None:
    from app.celery_app import celery_app

    celery_app.worker_main(argv=argv)


def worker_esxi() -> None:
    """Run only the esxi-queue worker (multi-host deploys)."""
    _run_worker(_esxi_worker_argv())


def worker_provision() -> None:
    """Run only the provision-queue worker (multi-host deploys)."""
    _run_worker(_provision_worker_argv())


def worker() -> None:
    """One-command dev entrypoint: launch both queue workers as children.

    Distinct ``-n`` nodenames avoid a collision on one host. The parent exits
    (terminating the survivor) as soon as either child dies, so a wedged half
    never lingers unnoticed; Ctrl-C tears both down.
    """
    children = [
        multiprocessing.Process(
            target=_run_worker, args=(_esxi_worker_argv(),), name="esxi-worker"
        ),
        multiprocessing.Process(
            target=_run_worker,
            args=(_provision_worker_argv(),),
            name="provision-worker",
        ),
    ]
    for child in children:
        child.start()
    exit_code = 0
    try:
        while all(child.is_alive() for child in children):
            time.sleep(1)
        exit_code = next(
            (child.exitcode or 0) for child in children if not child.is_alive()
        )
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
        for child in children:
            child.join()
    sys.exit(exit_code)


def admin_exists() -> None:
    """Exit 0 if any admin account already exists, 1 otherwise (silent).

    Lets ``deploy/prod-deploy.sh`` decide whether the first-admin bootstrap is
    needed *before* prompting: on a redeploy an admin is already present, so the
    deploy runs unattended with no credential prompt. Checks for *any* admin
    (not a specific username) so a first deploy that named its admin something
    other than the default still counts as bootstrapped on later runs.
    """
    from pymongo import MongoClient

    from app.core.settings import settings

    client: MongoClient = MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000)
    try:
        present = (
            client[settings.mongo_db]["users"].find_one({"role": "admin"}) is not None
        )
    finally:
        client.close()
    sys.exit(0 if present else 1)


def create_admin() -> None:
    """Bootstrap CLI: provision an account (``uv run create-admin``), any role.

    Exists because account creation is otherwise gated behind an admin
    session — a fresh deploy has none to mint one. Sync PyMongo on purpose:
    this runs outside the API process/event loop.

    Non-interactive mode (used by ``deploy/prod-deploy.sh`` to seed the first
    admin account on every deploy run): set ``ADMIN_PASSWORD`` in the
    environment to skip the interactive prompt/confirmation. Either way, an
    existing username is treated as already-provisioned and left untouched
    (idempotent, so re-running deploy never errors or overwrites a password).
    """
    from pymongo import MongoClient
    from pymongo.errors import DuplicateKeyError

    from app.core.db.models import UserDoc, now_ms, to_mongo
    from app.core.identity import hash_password
    from app.core.settings import settings

    parser = argparse.ArgumentParser(description="Provision an account.")
    parser.add_argument("username")
    parser.add_argument("--email", default=None)
    parser.add_argument(
        "--role", choices=("admin", "operator", "guest"), default="operator"
    )
    args = parser.parse_args()

    env_password = os.environ.get("ADMIN_PASSWORD")
    if env_password is not None:
        password = env_password
        if len(password) < 8:
            sys.exit("ADMIN_PASSWORD must be at least 8 characters.")
    else:
        password = getpass.getpass("Password: ")
        if len(password) < 8:
            sys.exit("Password must be at least 8 characters.")
        if getpass.getpass("Repeat password: ") != password:
            sys.exit("Passwords do not match.")

    doc = UserDoc(
        id=args.username,
        username=args.username,
        email=args.email,
        password_hash=hash_password(password),
        role=args.role,
        auth="local",
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    client: MongoClient = MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000)
    try:
        if client[settings.mongo_db]["users"].find_one({"username": args.username}):
            print(f"User '{args.username}' already exists — leaving it untouched.")
            return
        client[settings.mongo_db]["users"].insert_one(to_mongo(doc))
    except DuplicateKeyError:
        print(f"User '{args.username}' already exists — leaving it untouched.")
        return
    finally:
        client.close()
    print(f"Created {args.role} account '{args.username}'.")
