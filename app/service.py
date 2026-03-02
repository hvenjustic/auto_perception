from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.models import (
    DeleteMonitorByUrlResponse,
    DetectedChange,
    ImportSitesRequest,
    ImportSitesResponse,
    MonitorUrlListResponse,
    PersistedState,
    ResultChangeItem,
    RunScanRequest,
    ScanResultResponse,
    RunScanResponse,
    SiteConfig,
    SiteRuntimeState,
    utc_now_iso,
)
from app.monitor import WebsiteMonitor
from app.storage import StateStore
from app.summarizer import ChangeSummarizer


class AutoPerceptionService:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.monitor = WebsiteMonitor()
        self.summarizer = ChangeSummarizer()

    def import_sites(self, request: ImportSitesRequest) -> ImportSitesResponse:
        state = self.store.load()
        if request.replace_all:
            state = PersistedState()

        imported_ids: list[str] = []
        now = utc_now_iso()
        seen_ids: set[str] = set()

        for site in request.sites:
            site_id = site.site_id or self._build_site_id(site.url, state.sites, seen_ids)
            if site_id in seen_ids:
                raise ValueError(f"请求中 site_id 重复: {site_id}")
            seen_ids.add(site_id)

            old = state.sites.get(site_id)
            created_at = old.created_at if old else now
            site_name = site.name or (old.name if old else site_id)
            state.sites[site_id] = SiteConfig(
                site_id=site_id,
                name=site_name,
                url=site.url,
                content_selector=site.content_selector,
                enabled=site.enabled,
                max_pages_per_scan=site.max_pages_per_scan,
                created_at=created_at,
                updated_at=now,
            )
            state.runtime.setdefault(site_id, SiteRuntimeState())
            imported_ids.append(site_id)

        self.store.save(state)
        return ImportSitesResponse(
            imported_count=len(imported_ids),
            site_ids=imported_ids,
            replaced_all=request.replace_all,
        )

    def list_sites(self) -> list[SiteConfig]:
        state = self.store.load()
        return sorted(state.sites.values(), key=lambda x: x.site_id)

    def list_monitor_urls(self) -> MonitorUrlListResponse:
        state = self.store.load()
        urls = sorted({site.url for site in state.sites.values() if site.enabled})
        return MonitorUrlListResponse(urls=urls)

    def delete_monitor_by_url(self, url: str) -> DeleteMonitorByUrlResponse:
        state = self.store.load()
        matched_site_ids = [sid for sid, site in state.sites.items() if site.url == url]
        if not matched_site_ids:
            raise ValueError("条目不存在或已删除")

        matched_set = set(matched_site_ids)
        for sid in matched_site_ids:
            state.sites.pop(sid, None)
            state.runtime.pop(sid, None)
        state.changes = [x for x in state.changes if x.site_id not in matched_set]
        self.store.save(state)

        return DeleteMonitorByUrlResponse(
            deleted_count=len(matched_site_ids),
            deleted_site_ids=matched_site_ids,
            url=url,
            message="删除成功",
        )

    def list_changes(self, site_id: str | None = None, limit: int = 50) -> list[DetectedChange]:
        state = self.store.load()
        changes = state.changes
        if site_id:
            changes = [c for c in changes if c.site_id == site_id]
        return changes[:limit]

    def get_change(self, change_id: str) -> DetectedChange | None:
        state = self.store.load()
        for change in state.changes:
            if change.change_id == change_id:
                return change
        return None

    def get_latest_result(self) -> ScanResultResponse:
        state = self.store.load()
        raw = state.meta.get("latest_result")
        if not isinstance(raw, dict):
            return ScanResultResponse()
        try:
            return ScanResultResponse.model_validate(raw)
        except ValueError:
            return ScanResultResponse()

    async def run_scan(self, request: RunScanRequest) -> RunScanResponse:
        state = self.store.load()
        site_ids = self._resolve_scan_site_ids(request, state)
        scan_result = ScanResultResponse(
            task_start_time=utc_now_iso(),
            task_end_time=None,
            monitor_count=len(site_ids),
            detected_count=0,
            pending_count=len(site_ids),
            changes=[],
        )
        state.meta["latest_result"] = scan_result.model_dump(mode="json")
        self.store.save(state)

        if not site_ids:
            scan_result.task_end_time = utc_now_iso()
            scan_result.pending_count = 0
            state.meta["latest_result"] = scan_result.model_dump(mode="json")
            self.store.save(state)
            return RunScanResponse(scanned_site_ids=[], changes=[])

        all_changes: list[DetectedChange] = []
        async with httpx.AsyncClient() as client:
            for site_id in site_ids:
                site = state.sites[site_id]
                runtime = state.runtime.get(site_id, SiteRuntimeState())
                first_scan = runtime.last_scan_at is None

                layer1 = await self.monitor.layer1(client, site, runtime)
                candidates = set(layer1.candidate_urls)
                if not candidates:
                    candidates.add(site.url)

                layer2 = await self.monitor.layer2(client, site, runtime, candidates)

                runtime.rss_items = layer1.rss_items
                runtime.sitemap_lastmod = layer1.sitemap_lastmod
                runtime.index_hash = layer1.index_hash
                runtime.page_hashes = layer2.page_hashes
                runtime.page_snapshots = layer2.page_snapshots
                runtime.last_scan_at = datetime.now(timezone.utc).isoformat()
                state.runtime[site_id] = runtime

                for page_change in layer2.changes:
                    if first_scan and page_change.old_hash is None:
                        continue
                    summary = await self.summarizer.summarize(
                        page_url=page_change.page_url,
                        old_text=page_change.old_text,
                        new_text=page_change.new_text,
                    )
                    detected = DetectedChange(
                        change_id=uuid.uuid4().hex,
                        site_id=site_id,
                        site_name=site.name,
                        page_url=page_change.page_url,
                        layer="layer3_summary",
                        change_type=page_change.change_type,
                        old_hash=page_change.old_hash,
                        new_hash=page_change.new_hash,
                        summary=summary,
                        signals=layer1.signals[:10],
                    )
                    all_changes.append(detected)
                    scan_result.changes.append(
                        ResultChangeItem(url=detected.page_url, summary=detected.summary)
                    )

                scan_result.detected_count += 1
                scan_result.pending_count = max(
                    0, scan_result.monitor_count - scan_result.detected_count
                )
                state.meta["latest_result"] = scan_result.model_dump(mode="json")
                self.store.save(state)

        if all_changes:
            state.changes = all_changes + state.changes
            state.changes = state.changes[:5000]
        scan_result.task_end_time = utc_now_iso()
        scan_result.pending_count = max(
            0, scan_result.monitor_count - scan_result.detected_count
        )
        state.meta["latest_result"] = scan_result.model_dump(mode="json")
        self.store.save(state)

        return RunScanResponse(scanned_site_ids=site_ids, changes=all_changes)

    def _resolve_scan_site_ids(
        self, request: RunScanRequest, state: PersistedState
    ) -> list[str]:
        if request.site_ids:
            valid_ids = [x for x in request.site_ids if x in state.sites]
            return [x for x in valid_ids if state.sites[x].enabled]
        return [sid for sid, site in state.sites.items() if site.enabled]

    def _build_site_id(
        self, url: str, current_sites: dict[str, SiteConfig], current_batch: set[str]
    ) -> str:
        parsed = urlparse(url)
        base = (parsed.netloc or "site").replace(".", "_").replace("-", "_")
        candidate = base
        seq = 1
        while candidate in current_sites or candidate in current_batch:
            candidate = f"{base}_{seq}"
            seq += 1
        return candidate
