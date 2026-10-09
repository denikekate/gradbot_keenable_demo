"""Combined app that mounts all demos under /<demo_name>/."""

import contextlib
import importlib
import os
import sys
from pathlib import Path

from fastapi import FastAPI

DEMOS_DIR = Path(__file__).parent

# Discover and import each demo before constructing the parent app, so
# the parent's lifespan can chain into each child's lifespan (FastAPI
# does not propagate lifespans through `app.mount(...)`).
demo_names = sorted(
    d.name
    for d in DEMOS_DIR.iterdir()
    if d.is_dir() and (d / "main.py").exists()
)

# DEMOS="a,b" serves only those demos. Other demos may need their own secrets
# (paris_rental_agent wants SECRET_KEY, hotel wants a Linkup key) and would
# otherwise crash the whole app at startup.
_only = [n.strip() for n in os.environ.get("DEMOS", "").split(",") if n.strip()]
if _only:
    demo_names = [n for n in demo_names if n in _only]

_demos: list[tuple[str, FastAPI]] = []
for name in demo_names:
    demo_path = DEMOS_DIR / name
    sys.path.insert(0, str(demo_path))
    try:
        mod = importlib.import_module(f"{name}.main")
        demo_app = getattr(mod, "app", None)
        if demo_app is not None:
            _demos.append((name, demo_app))
    except Exception as e:
        print(f"Warning: could not load demo '{name}': {e}")
    finally:
        sys.path.pop(0)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    async with contextlib.AsyncExitStack() as stack:
        for name, demo_app in _demos:
            try:
                await stack.enter_async_context(
                    demo_app.router.lifespan_context(demo_app)
                )
            except Exception as e:  # one demo's startup must not take the others down
                print(f"Warning: demo '{name}' failed to start: {e}")
        yield


app = FastAPI(title="Gradbot Demos", lifespan=_lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def index():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/{_demos[0][0]}/" if _demos else "/healthz")


for name, demo_app in _demos:
    app.mount(f"/{name}", demo_app)
