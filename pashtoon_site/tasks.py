"""Background jobs, run by one thread every minute: held phone alerts, the daily backup, tiffin orders. Run the site
as a single process (the default) so these don't run twice."""
import logging
import threading
import time

from . import backup, config, gcal, notify

log = logging.getLogger(__name__)
_started = False


def tick() -> None:
    from . import tiffin
    for job in (notify.flush_held, backup.maybe_run, tiffin.extend_all, gcal.sync_pending):
        try:
            job()
        except Exception:  # noqa: BLE001  (one failing job must not stop the others)
            log.exception("background job %s failed", job.__name__)


def start() -> None:
    global _started
    if _started or not config.BACKGROUND:
        return
    _started = True

    def loop():
        time.sleep(5)
        while True:
            tick()
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="pashtoon-tasks").start()
