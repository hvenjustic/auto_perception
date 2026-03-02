from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url 必须是 http/https 且包含域名")
    normalized = parsed._replace(fragment="").geturl()
    return normalized.rstrip("/")


class SiteConfigInput(BaseModel):
    site_id: str | None = Field(default=None, description="站点唯一 ID，不传时自动生成")
    name: str | None = Field(default=None, description="站点名称")
    url: str = Field(description="站点主页 URL")
    content_selector: str | None = Field(
        default=None, description="深度扫描时优先抽取的 CSS Selector"
    )
    enabled: bool = Field(default=True, description="是否启用监控")
    max_pages_per_scan: int = Field(default=10, ge=1, le=100)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _normalize_url(value)


class SiteConfig(BaseModel):
    site_id: str
    name: str
    url: str
    content_selector: str | None = None
    enabled: bool = True
    max_pages_per_scan: int = Field(default=10, ge=1, le=100)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class ImportSitesRequest(BaseModel):
    sites: list[SiteConfigInput] = Field(default_factory=list)
    replace_all: bool = Field(default=False, description="true 时替换已有全部站点配置")


class ImportSitesResponse(BaseModel):
    imported_count: int
    site_ids: list[str]
    replaced_all: bool


class RunScanRequest(BaseModel):
    site_ids: list[str] | None = Field(
        default=None, description="不传时扫描所有启用站点"
    )


class SiteRuntimeState(BaseModel):
    rss_items: dict[str, str] = Field(default_factory=dict)
    sitemap_lastmod: dict[str, str] = Field(default_factory=dict)
    index_hash: str | None = None
    page_hashes: dict[str, str] = Field(default_factory=dict)
    page_snapshots: dict[str, str] = Field(default_factory=dict)
    last_scan_at: str | None = None


class DetectedChange(BaseModel):
    change_id: str
    site_id: str
    site_name: str
    page_url: str
    layer: str
    change_type: str
    old_hash: str | None = None
    new_hash: str | None = None
    summary: str
    signals: list[str] = Field(default_factory=list)
    detected_at: str = Field(default_factory=utc_now_iso)


class RunScanResponse(BaseModel):
    scanned_site_ids: list[str]
    changes: list[DetectedChange]


class MonitorUrlListResponse(BaseModel):
    urls: list[str]


class DeleteMonitorByUrlRequest(BaseModel):
    url: str = Field(description="需要删除的监测 URL")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _normalize_url(value)


class DeleteMonitorByUrlResponse(BaseModel):
    deleted_count: int
    deleted_site_ids: list[str]
    url: str
    message: str


class ResultChangeItem(BaseModel):
    url: str
    summary: str


class ScanResultResponse(BaseModel):
    task_start_time: str | None = None
    task_end_time: str | None = None
    monitor_count: int = 0
    detected_count: int = 0
    pending_count: int = 0
    changes: list[ResultChangeItem] = Field(default_factory=list)


class PersistedState(BaseModel):
    sites: dict[str, SiteConfig] = Field(default_factory=dict)
    runtime: dict[str, SiteRuntimeState] = Field(default_factory=dict)
    changes: list[DetectedChange] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
