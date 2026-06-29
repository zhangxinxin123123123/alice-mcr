"""Gunicorn startup hook for the Alice settlement patch.

Render usually starts this app through Gunicorn's console script, so Python does
not always auto-import the repository-level sitecustomize.py. Loading it here
makes the settlement update active in each worker after deploy.
"""


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


def when_ready(server):
    _patch(getattr(server, "log", None))


def post_worker_init(worker):
    _patch(getattr(worker, "log", None))
