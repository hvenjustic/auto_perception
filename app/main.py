from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query

from app.env_loader import load_env_file
from app.models import (
    DeleteMonitorByUrlRequest,
    DeleteMonitorByUrlResponse,
    ImportSitesRequest,
    ImportSitesResponse,
    MonitorUrlListResponse,
    RunScanRequest,
    ScanResultResponse,
    ScanTaskStatusResponse,
    SiteConfig,
)
from app.service import AutoPerceptionService
from app.storage import StateStore

ENV_FILE = os.getenv("AUTO_PERCEPTION_ENV_FILE", "config/runtime.env")
load_env_file(ENV_FILE)

DB_FILE = os.getenv("AUTO_PERCEPTION_DB_FILE", "data/state.db")
LEGACY_STATE_FILE = os.getenv("AUTO_PERCEPTION_LEGACY_STATE_FILE", "data/state.json")
store = StateStore(DB_FILE, legacy_state_file=LEGACY_STATE_FILE)
service = AutoPerceptionService(store)

app = FastAPI(
    title="Auto Perception API",
    version="1.0.0",
    description="自动感知网站变化服务，支持配置导入、变化检测、摘要输出。",
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/config/import", response_model=ImportSitesResponse)
def import_sites(payload: ImportSitesRequest) -> ImportSitesResponse:
    if not payload.sites:
        raise HTTPException(status_code=400, detail="sites 不能为空")
    try:
        return service.import_sites(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/config/sites", response_model=list[SiteConfig])
def list_sites() -> list[SiteConfig]:
    return service.list_sites()


@app.get("/api/v1/monitor/urls", response_model=MonitorUrlListResponse)
def list_monitor_urls() -> MonitorUrlListResponse:
    return service.list_monitor_urls()


@app.delete("/api/v1/monitor/by-url", response_model=DeleteMonitorByUrlResponse)
def delete_monitor_by_url(payload: DeleteMonitorByUrlRequest) -> DeleteMonitorByUrlResponse:
    try:
        return service.delete_monitor_by_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/scan/run", response_model=ScanTaskStatusResponse)
async def run_scan(payload: RunScanRequest) -> ScanTaskStatusResponse:
    return await service.start_scan(payload)


@app.get("/api/v1/scan/status", response_model=ScanTaskStatusResponse)
def get_scan_status() -> ScanTaskStatusResponse:
    return service.get_scan_status()


@app.post("/api/v1/scan/stop", response_model=ScanTaskStatusResponse)
def stop_scan() -> ScanTaskStatusResponse:
    try:
        return service.stop_scan()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/result", response_model=ScanResultResponse)
def get_result() -> ScanResultResponse:
    return service.get_latest_result()


@app.get("/api/v1/changes")
def list_changes(
    site_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    return service.list_changes(site_id=site_id, limit=limit)


@app.get("/api/v1/changes/{change_id}")
def get_change(change_id: str):
    data = service.get_change(change_id)
    if data is None:
        raise HTTPException(status_code=404, detail="change_id 不存在")
    return data
