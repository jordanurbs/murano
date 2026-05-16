"""Background scheduler for the long-running server.

Two opt-in background workers run inside `murano serve`:

1. Nightly summary-tree rebuild via apscheduler (default 03:00 local time).
2. Vault file watcher via watchfiles in a daemon thread, so dropped notes
   become searchable without a separate `murano watch` process.

Both are no-ops if Venice is unreachable; failures are logged to
`~/.murano/logs/scheduler.log` and swallowed so the HTTP server keeps
serving.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from datetime import time as time_class
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config import Settings
from ..tree.build import build_tree
from ..vault.watcher import watch_vault

NIGHTLY_DEFAULT_HOUR = 3
NIGHTLY_DEFAULT_MINUTE = 0
LOG_FILENAME = "scheduler.log"


@dataclass
class SchedulerHandles:
    """Returned from `start_background_workers` so the lifespan can stop them cleanly."""

    scheduler: BackgroundScheduler | None
    watcher_thread: threading.Thread | None
    stop_event: threading.Event | None


def _build_logger(settings: Settings) -> logging.Logger:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("murano.scheduler")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        settings.logs_dir / LOG_FILENAME, maxBytes=2_000_000, backupCount=3
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _rebuild_job(settings: Settings) -> None:
    logger = _build_logger(settings)
    logger.info("Nightly tree rebuild starting...")
    try:
        report = build_tree(settings, progress=lambda m: logger.info(m))
        if report.skipped_reason:
            logger.warning("Tree rebuild skipped: %s", report.skipped_reason)
        else:
            logger.info(
                "Tree rebuild complete: %d nodes, %d edges, %.1fs",
                report.total_nodes,
                report.total_edges,
                report.elapsed_seconds,
            )
    except Exception as e:  # pragma: no cover (background error path)
        logger.exception("Tree rebuild failed: %s", e)


def _start_watcher_thread(settings: Settings) -> tuple[threading.Thread, threading.Event]:
    logger = _build_logger(settings)
    stop_event = threading.Event()

    def progress(file_result) -> None:  # noqa: ANN001
        if file_result.status == "error":
            logger.warning("Watcher error on %s: %s", file_result.relpath, file_result.error)

    def on_batch(paths, report) -> None:  # noqa: ANN001
        if report.errors:
            for err in report.errors:
                logger.warning("Index error on %s: %s", err.relpath, err.error)
        if report.files_indexed or report.files_removed:
            logger.info(
                "Watcher reindexed %s (added=%d, removed=%d, chunks=%d)",
                sorted(p.as_posix() for p in paths),
                report.files_indexed,
                report.files_removed,
                report.chunks_inserted,
            )

    def run() -> None:
        try:
            watch_vault(
                settings,
                progress=progress,
                on_batch=on_batch,
                stop_event=stop_event,
            )
        except Exception as e:  # pragma: no cover
            logger.exception("Watcher crashed: %s", e)

    thread = threading.Thread(target=run, name="murano-watcher", daemon=True)
    thread.start()
    logger.info("Vault watcher thread started on %s", settings.vault_root)
    return thread, stop_event


def start_background_workers(
    settings: Settings,
    *,
    enable_schedule: bool = True,
    enable_watch: bool = True,
    nightly_hour: int = NIGHTLY_DEFAULT_HOUR,
    nightly_minute: int = NIGHTLY_DEFAULT_MINUTE,
) -> SchedulerHandles:
    """Boot the optional background workers. Returns handles for clean shutdown."""
    logger = _build_logger(settings)

    scheduler: BackgroundScheduler | None = None
    if enable_schedule:
        scheduler = BackgroundScheduler(timezone="local")
        scheduler.add_job(
            _rebuild_job,
            CronTrigger(hour=nightly_hour, minute=nightly_minute),
            args=[settings],
            id="nightly_tree_rebuild",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        next_run = datetime.combine(
            datetime.now().date(), time_class(nightly_hour, nightly_minute)
        )
        logger.info("Scheduler started; nightly tree rebuild at %s", next_run.time())

    watcher_thread: threading.Thread | None = None
    stop_event: threading.Event | None = None
    if enable_watch:
        try:
            watcher_thread, stop_event = _start_watcher_thread(settings)
        except FileNotFoundError as e:
            logger.warning("Watcher not started: %s", e)

    return SchedulerHandles(
        scheduler=scheduler, watcher_thread=watcher_thread, stop_event=stop_event
    )


def stop_background_workers(handles: SchedulerHandles) -> None:
    """Clean shutdown — called from the FastAPI lifespan."""
    if handles.scheduler is not None:
        handles.scheduler.shutdown(wait=False)
    if handles.stop_event is not None:
        handles.stop_event.set()


def kill_port(port: int) -> int:
    """Best-effort port-killer for `murano serve --restart` (and scripts/dev.sh).

    Returns the count of processes killed. Failures are debug-logged so
    `--restart didn't free port 3000` is diagnosable; we never raise from
    here because the caller is about to try binding the port anyway and
    will get a clearer error if the kill genuinely failed.
    """
    import platform
    import signal
    import subprocess

    _kp_log = logging.getLogger("murano.kill_port")
    system = platform.system()
    killed = 0
    if system in {"Darwin", "Linux"}:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                check=False,
                capture_output=True,
                text=True,
            )
            pids = [int(p) for p in out.stdout.strip().splitlines() if p.strip().isdigit()]
            for pid in pids:
                try:
                    import os

                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                    _kp_log.debug("killed pid %d holding port %d", pid, port)
                except (ProcessLookupError, PermissionError) as e:
                    _kp_log.debug("could not kill pid %d on port %d: %s", pid, port, e)
        except FileNotFoundError:
            _kp_log.debug("lsof not available; cannot kill port %d", port)
    elif system == "Windows":  # pragma: no cover
        try:
            out = subprocess.run(
                ["netstat", "-ano"], check=False, capture_output=True, text=True
            )
            for line in out.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid], check=False, capture_output=True
                    )
                    killed += 1
                    _kp_log.debug("killed pid %s holding port %d (windows)", pid, port)
        except FileNotFoundError:
            _kp_log.debug("netstat not available; cannot kill port %d", port)

    # We deliberately do NOT run `pkill -f 'murano serve'` as a fallback,
    # even though earlier versions did. The substring match was too broad:
    # an editor with a file open, a `grep`, or another shell containing the
    # string "murano serve" would all be killed. lsof-on-port above is
    # sufficient for the port-conflict case the user actually cares about.
    return killed


def _ensure_signature() -> None:
    """Defensive imports check (called by tests)."""
    assert callable(start_background_workers)
    assert callable(stop_background_workers)
    assert callable(kill_port)
