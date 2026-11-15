"""Wait for a sheet, capture both sides, and publish one complete PDF via driverless eSCL."""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
import signal
from threading import Event
import time

from .config import DPI, Settings
from .escl import (
    EsclError,
    EsclScanner,
    PaperJammedError,
    ScannerBusyError,
    ScannerOfflineError,
)
from .spool import ScanSpool, atomic_json, build_pdf

LOG = logging.getLogger(__name__)


def capture_if_ready_escl(
    scanner: EsclScanner, spool: ScanSpool, paper_size: str, on_scan
) -> str | None:
    status = scanner.get_status()
    if status.is_jammed:
        raise PaperJammedError("Paper jammed in scanner feeder")
    if not status.is_paper_loaded:
        return None

    folder = spool.new_scan()
    manifest_path = folder / "manifest.json"
    manifest = {
        "id": folder.name,
        "phase": "scanning",
        "dpi": DPI,
        "duplex": True,
        "paper_size": paper_size,
    }
    atomic_json(manifest_path, manifest, durable=True)
    try:
        on_scan()
        job_url = scanner.start_scan_job(duplex=True, dpi=DPI)
        pages = scanner.fetch_job_pages(job_url, folder)
        if not pages:
            raise EsclError("No pages received from scanner job")
        build_pdf(pages, folder / "document.pdf")
        manifest.update(phase="ready", pages=len(pages))
        atomic_json(manifest_path, manifest, durable=True)
        spool.publish(folder)
    except Exception as error:
        if manifest.get("phase") != "ready":
            manifest.update(phase="error", error=str(error))
            atomic_json(manifest_path, manifest, durable=True)
        raise
    return folder.name


def retry_state_escl(error: Exception) -> tuple[str, int]:
    if isinstance(error, ScannerOfflineError):
        return "scanner_offline", 15
    if isinstance(error, ScannerBusyError):
        return "scanner_busy", 30
    if isinstance(error, PaperJammedError):
        return "paper_blocked", 20
    return "retry_after_error", 10


def serve(settings: Settings, stop: Event):
    def status(state, **details):
        atomic_json(
            settings.work / "auto-status.json",
            {
                "updated": time.time(),
                "state": state,
                "paper_size": settings.paper_size,
                **details,
            },
        )

    settings.configure_driver()
    settings.work.mkdir(parents=True, exist_ok=True)
    settings.output.mkdir(parents=True, exist_ok=True)

    spool = ScanSpool(
        settings.work,
        settings.output,
        gemini_api_key=settings.gemini_api_key,
        enable_ai_filing=settings.enable_ai_filing,
    )
    scanner = EsclScanner(settings.scanner_ip)
    status("connecting")
    previous_state = None
    next_recovery = 0.0

    LOG.info("Starting eSCL receiver for Brother DS-940DW at %s", settings.scanner_ip)

    try:
        while not stop.is_set():
            if time.monotonic() >= next_recovery:
                spool.recover()
                next_recovery = time.monotonic() + 30
            try:
                scanned_id = capture_if_ready_escl(
                    scanner, spool, settings.paper_size, lambda: status("scanning")
                )
                if scanned_id:
                    LOG.info("Successfully scanned and published job: %s", scanned_id)

                if previous_state != "waiting_for_paper":
                    LOG.info("Ready for paper at %s (duplex, %d dpi)", settings.scanner_ip, DPI)
                previous_state = "waiting_for_paper"
                status(previous_state)
                stop.wait(3)
            except (OSError, RuntimeError, ValueError, KeyError, EsclError) as error:
                state, delay = retry_state_escl(error)
                if state != previous_state or state == "retry_after_error":
                    LOG.warning("%s. Retrying in %d seconds (%s).", error, delay, state)
                previous_state = state
                status(state, retry_seconds=delay)
                if state == "retry_after_error":
                    stop.wait(delay)
                    continue
                deadline = time.monotonic() + delay
                while not stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
                    if stop.wait(min(5, remaining)):
                        break
                    status(state)
    finally:
        status("stopped")


def main():
    os.umask(0o007)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_environment()
    settings.work.mkdir(parents=True, exist_ok=True)
    settings.output.mkdir(parents=True, exist_ok=True)
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    with (settings.work / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("A worker already owns this work directory") from error
        serve(settings, stop)


if __name__ == "__main__":
    main()
