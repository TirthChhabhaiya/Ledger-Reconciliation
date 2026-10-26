"""FastAPI backend (Vercel Python function) for the Ledger Reconciliation app."""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

import _pipeline

app = FastAPI(title="Ledger Reconciliation API")
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/run")
async def run(request: Request):
    """One rerun of the page: READ LEDGERS and/or display for the current settings."""
    form = await request.form()
    try:
        params = json.loads(form["params"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail="Missing or invalid params.")

    inline_files = {
        key: await value.read()
        for key, value in form.multi_items()
        if isinstance(value, UploadFile)
    }

    try:
        result = await run_in_threadpool(_pipeline.run, params, inline_files)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": f"{type(e).__name__}: {e}"})

    return JSONResponse(result)
