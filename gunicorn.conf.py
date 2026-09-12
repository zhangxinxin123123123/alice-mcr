"""Memory-conscious Gunicorn settings and Alice settlement patch hook.

Render usually starts this app through Gunicorn's console script, so Python does
not always auto-import the repository-level sitecustomize.py. Loading it here
makes the settlement update active in each worker after deploy.
"""

import os

# Render's smaller instances are much more stable with one process. Flask I/O
# still has four request threads, while avoiding a full copy of pandas/Pillow,
# the application module and in-memory response buffers in every worker.
workers = max(1, min(2, int(os.environ.get("ALICE_WEB_CONCURRENCY", "1"))))
worker_class = "gthread"
threads = max(2, min(6, int(os.environ.get("ALICE_WEB_THREADS", "2"))))
preload_app = False
timeout = 120
graceful_timeout = 30
keepalive = 5

# Recycle the sole worker periodically so native image/font allocations cannot
# accumulate indefinitely. Jitter avoids a predictable restart boundary.
max_requests = 80
max_requests_jitter = 20


def _patch(log=None):
    try:
        import importlib

        app_module = importlib.import_module("app")
        patch_module = importlib.import_module("sitecustomize")
        install = getattr(patch_module, "_install", None)
        if install is None:
            raise RuntimeError("sitecustomize._install is missing")
        install(app_module)
        if log:
            log.info("Alice settlement patch loaded")
    except Exception as exc:
        if log:
            log.warning("Alice settlement patch failed: %s", exc)
        else:
            raise


def post_worker_init(worker):
    _patch(getattr(worker, "log", None))
