from __future__ import annotations

import asyncio
import threading
import uuid
from collections import deque
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
    ScanTaskStatusResponse,
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
        self._task_lock = threading.Lock()
        self._scan_running = False
        self._stop_requested = False
        self._current_task_id: str | None = None
        self._last_status = "idle"
        self._last_message = "暂无运行中的扫描任务"
        self._scan_queue: deque[str] = deque()
        self._queued_or_running_site_ids: set[str] = set()

    def import_sites(self, request: ImportSitesRequest) -> ImportSitesResponse:
        state = self.store.load()
        if request.replace_all:
            state = PersistedState()
            self._reset_queue()

        imported_ids: list[str] = []
        baseline_queue_ids: list[str] = []
        now = utc_now_iso()
        seen_ids: set[str] = set()
        url_owner: dict[str, str] = {cfg.url: sid for sid, cfg in state.sites.items()}

        for site in request.sites:
            site_id = site.site_id or self._build_site_id(site.url, state.sites, seen_ids)
            if site_id in seen_ids:
                raise ValueError(f"请求中 site_id 重复: {site_id}")
            seen_ids.add(site_id)

            old = state.sites.get(site_id)
            if old and url_owner.get(old.url) == site_id:
                url_owner.pop(old.url, None)
            owner = url_owner.get(site.url)
            if owner is not None and owner != site_id:
                raise ValueError(f"url 已存在: {site.url}")
            url_owner[site.url] = site_id

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
            if old is None or old.url != site.url:
                baseline_queue_ids.append(site_id)

        self.store.save(state)
        self._enqueue_site_ids(baseline_queue_ids)
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
        removed_from_queue = self._remove_site_ids_from_queue(matched_set)
        if removed_from_queue > 0:
            state = self.store.load()
            scan_result = self._load_latest_result(state)
            scan_result.monitor_count = max(0, scan_result.monitor_count - removed_from_queue)
            scan_result.pending_count = max(0, scan_result.pending_count - removed_from_queue)
            state.meta["latest_result"] = scan_result.model_dump(mode="json")
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
        return self._load_latest_result(state)

    def get_scan_status(self) -> ScanTaskStatusResponse:
        state = self.store.load()
        latest = self._load_latest_result(state)
        with self._task_lock:
            running = self._scan_running
            stop_requested = self._stop_requested
            current_task_id = self._current_task_id
            status = "running" if running else self._last_status
            message = self._last_message

        elapsed = self._calc_elapsed_seconds(
            latest.task_start_time,
            None if running else latest.task_end_time,
        )
        progress = self._calc_progress(latest.monitor_count, latest.detected_count)
        return ScanTaskStatusResponse(
            task_id=current_task_id or latest.task_id,
            status=status,
            task_start_time=latest.task_start_time,
            task_end_time=latest.task_end_time,
            elapsed_seconds=elapsed,
            monitor_count=latest.monitor_count,
            detected_count=latest.detected_count,
            pending_count=latest.pending_count,
            progress=progress,
            stop_requested=stop_requested,
            message=message,
        )

    async def start_scan(self, request: RunScanRequest) -> ScanTaskStatusResponse:
        state = self.store.load()
        site_ids = self._resolve_scan_site_ids(request, state)
        added_count = self._enqueue_site_ids(site_ids)

        already_running = False
        with self._task_lock:
            if self._scan_running:
                already_running = True
                if added_count > 0:
                    self._last_message = f"任务运行中，新增 {added_count} 个站点已入队"
                else:
                    self._last_message = "任务运行中，请等待当前任务完成"
            else:
                task_id = uuid.uuid4().hex
                self._scan_running = True
                self._stop_requested = False
                self._current_task_id = task_id
                self._last_status = "running"
                self._last_message = "扫描任务已启动"
        if already_running:
            return self.get_scan_status()

        queue_size = self._queue_size()
        scan_result = ScanResultResponse(
            task_id=task_id,
            task_start_time=utc_now_iso(),
            task_end_time=None,
            monitor_count=queue_size,
            detected_count=0,
            pending_count=queue_size,
            changes=[],
        )
        state.meta["latest_result"] = scan_result.model_dump(mode="json")
        self.store.save(state)
        asyncio.create_task(self._run_scan_task(task_id))
        return self.get_scan_status()

    def stop_scan(self) -> ScanTaskStatusResponse:
        with self._task_lock:
            if not self._scan_running:
                raise ValueError("当前没有运行中的扫描任务")
            self._stop_requested = True
            self._last_message = "已收到停止请求，正在结束当前扫描任务"
        return self.get_scan_status()

    async def _run_scan_task(self, task_id: str) -> None:
        stopped = False
        try:
            async with httpx.AsyncClient() as client:
                while True:
                    if self._should_stop(task_id):
                        stopped = True
                        break

                    site_id = self._dequeue_site_id(task_id)
                    if site_id is None:
                        break

                    try:
                        state = self.store.load()
                        site = state.sites.get(site_id)
                        if site is None or not site.enabled:
                            self._mark_site_detected(state)
                            continue

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

                        new_changes: list[DetectedChange] = []
                        scan_result = self._load_latest_result(state)
                        for page_change in layer2.changes:
                            if self._should_stop(task_id):
                                stopped = True
                                break
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
                            new_changes.append(detected)
                            scan_result.changes.append(
                                ResultChangeItem(url=detected.page_url, summary=detected.summary)
                            )

                        if new_changes:
                            state.changes = new_changes + state.changes
                            state.changes = state.changes[:5000]

                        self._mark_site_detected(state)
                        if stopped:
                            break
                    except Exception:
                        state = self.store.load()
                        self._mark_site_detected(state)
                    finally:
                        self._finish_site_schedule(site_id)

            state = self.store.load()
            scan_result = self._load_latest_result(state)
            scan_result.task_end_time = utc_now_iso()
            scan_result.pending_count = self._queue_size()
            state.meta["latest_result"] = scan_result.model_dump(mode="json")
            self.store.save(state)
            if stopped:
                self._finish_task(task_id, "stopped", "任务已停止，已保留已扫描结果")
            else:
                self._finish_task(task_id, "completed", "任务已完成")
        except Exception as exc:
            state = self.store.load()
            scan_result = self._load_latest_result(state)
            scan_result.task_end_time = utc_now_iso()
            state.meta["latest_result"] = scan_result.model_dump(mode="json")
            self.store.save(state)
            self._finish_task(task_id, "failed", f"任务失败: {exc}")

    def _enqueue_site_ids(self, site_ids: list[str]) -> int:
        if not site_ids:
            return 0
        added_ids: list[str] = []
        with self._task_lock:
            for site_id in site_ids:
                if site_id in self._queued_or_running_site_ids:
                    continue
                self._scan_queue.append(site_id)
                self._queued_or_running_site_ids.add(site_id)
                added_ids.append(site_id)
            running = self._scan_running

        if running and added_ids:
            state = self.store.load()
            scan_result = self._load_latest_result(state)
            scan_result.monitor_count += len(added_ids)
            scan_result.pending_count += len(added_ids)
            state.meta["latest_result"] = scan_result.model_dump(mode="json")
            self.store.save(state)
        return len(added_ids)

    def _dequeue_site_id(self, task_id: str) -> str | None:
        with self._task_lock:
            if not self._scan_running or self._current_task_id != task_id:
                return None
            if not self._scan_queue:
                return None
            return self._scan_queue.popleft()

    def _finish_site_schedule(self, site_id: str) -> None:
        with self._task_lock:
            self._queued_or_running_site_ids.discard(site_id)

    def _reset_queue(self) -> None:
        with self._task_lock:
            self._scan_queue.clear()
            self._queued_or_running_site_ids.clear()

    def _queue_size(self) -> int:
        with self._task_lock:
            return len(self._scan_queue)

    def _mark_site_detected(self, state: PersistedState) -> None:
        scan_result = self._load_latest_result(state)
        scan_result.detected_count += 1
        scan_result.pending_count = max(0, scan_result.pending_count - 1)
        state.meta["latest_result"] = scan_result.model_dump(mode="json")
        self.store.save(state)

    def _remove_site_ids_from_queue(self, site_ids: set[str]) -> int:
        if not site_ids:
            return 0
        with self._task_lock:
            removed = 0
            rebuilt: deque[str] = deque()
            while self._scan_queue:
                site_id = self._scan_queue.popleft()
                if site_id in site_ids:
                    removed += 1
                    self._queued_or_running_site_ids.discard(site_id)
                    continue
                rebuilt.append(site_id)
            self._scan_queue = rebuilt
            return removed

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

    def _load_latest_result(self, state: PersistedState) -> ScanResultResponse:
        raw = state.meta.get("latest_result")
        if not isinstance(raw, dict):
            return ScanResultResponse()
        try:
            return ScanResultResponse.model_validate(raw)
        except ValueError:
            return ScanResultResponse()

    def _finish_task(self, task_id: str, status: str, message: str) -> None:
        with self._task_lock:
            if self._current_task_id != task_id:
                return
            self._scan_running = False
            self._stop_requested = False
            self._last_status = status
            self._last_message = message

    def _should_stop(self, task_id: str) -> bool:
        with self._task_lock:
            return (
                self._scan_running
                and self._current_task_id == task_id
                and self._stop_requested
            )

    def _calc_elapsed_seconds(self, start_time: str | None, end_time: str | None) -> int:
        if not start_time:
            return 0
        try:
            start_dt = datetime.fromisoformat(start_time)
            if end_time:
                end_dt = datetime.fromisoformat(end_time)
            else:
                end_dt = datetime.now(timezone.utc)
            value = int((end_dt - start_dt).total_seconds())
            return max(0, value)
        except ValueError:
            return 0

    def _calc_progress(self, monitor_count: int, detected_count: int) -> float:
        if monitor_count <= 0:
            return 100.0
        progress = (detected_count / monitor_count) * 100.0
        if progress < 0:
            return 0.0
        if progress > 100:
            return 100.0
        return round(progress, 2)
