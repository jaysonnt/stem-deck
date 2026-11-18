"""Stem Deck — proxy to pianokb on the 9950x5080 workstation.

pianokb turns the BOYI keyboard into a stem player. Its web UI (ui/server.py,
port 8765) serves what is playing and takes control commands; this router
forwards the console's requests to it over the tailnet.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from http_client import async_client

REMOTE_URL = os.environ.get("CMD_CENTER_STEMDECK_URL", "http://pianokb-host:8765").rstrip("/")

router = APIRouter(prefix="/api/stemdeck", tags=["stemdeck"])


async def _proxy(method: str, path: str, timeout: float = 8.0, **kwargs: Any) -> Any:
    try:
        async with async_client(timeout=timeout) as client:
            response = await client.request(method, f"{REMOTE_URL}{path}", **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(503, f"stem player on the 5080 unreachable: {exc}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(502, "stem player returned invalid JSON") from exc
    if response.status_code >= 400:
        detail = data.get("error") if isinstance(data, dict) else None
        raise HTTPException(response.status_code, detail or response.text[:300])
    return data


@router.get("/now")
async def now() -> Any:
    return await _proxy("GET", "/api/now", timeout=4.0)


@router.get("/state")
async def state() -> Any:
    return await _proxy("GET", "/api/state", timeout=20.0)


@router.get("/jobs/{job_id}")
async def job(job_id: str) -> Any:
    if not job_id.isalnum() or len(job_id) > 40:
        raise HTTPException(400, "bad job id")
    return await _proxy("GET", f"/api/jobs/{job_id}")


class ControlReq(BaseModel):
    cmd: str = Field(pattern="^(latch|speed|scrub|seek|song|stem)$")
    on: bool | None = None
    value: float | None = Field(default=None, ge=0.5, le=2.0)
    seconds: float | None = Field(default=None, ge=-3600, le=3600)
    song: str | None = Field(default=None, max_length=200)
    stem: str | None = Field(default=None, max_length=60)


@router.post("/control")
async def control(req: ControlReq) -> Any:
    return await _proxy("POST", "/api/control", json=req.model_dump(exclude_none=True))


class SongReq(BaseModel):
    song: str = Field(min_length=1, max_length=120)


@router.post("/bench")
async def bench(req: SongReq) -> Any:
    return await _proxy("POST", "/api/bench", json=req.model_dump())


@router.post("/unbench")
async def unbench(req: SongReq) -> Any:
    return await _proxy("POST", "/api/unbench", json=req.model_dump())


class SplitUrlReq(BaseModel):
    song: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2048)
    replace: bool = False


@router.post("/split-url")
async def split_url(req: SplitUrlReq) -> Any:
    return await _proxy("POST", "/api/split-url", json=req.model_dump(), timeout=30.0)


@router.post("/restart")
async def restart() -> Any:
    return await _proxy("POST", "/api/restart", timeout=40.0)
