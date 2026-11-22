"""Background polling of machine state, cached for the web pages.

Before this, the dashboard pinged every machine while rendering, so the page waited for
the slowest machine, and FOCAS status was fetched only when you opened one machine's own
page. One thread now keeps a cache and the pages read it.

FOCAS is polled deliberately gently. A control allows only a few FOCAS connections at
once and the limit belongs to the control, not to the client -- a monitoring tool on the
same network spends the same budget (docs/COMPANIONS.md). So the poller:

* polls a control no more often than `focas_interval`, which is separate from, and
  normally longer than, the reachability sweep;
* takes the same per-machine lock that transfers take, without waiting. A transfer always
  wins: the poll is skipped rather than queued, because stale status matters less than a
  program arriving late;
* skips a control that is not answering pings, so an unplugged machine costs one ping
  rather than a connection attempt that blocks until it times out;
* can be turned off for one machine (its FOCAS "poll" setting) or entirely.

Nothing is stored: this is current state, not history. For history, charts and OEE, run a
monitoring tool beside PunchTape (docs/COMPANIONS.md).
"""
import datetime
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, events, focas_client, system

log = logging.getLogger("punchtape")

# How long a cached entry may be before a page treats it as unknown rather than current.
STALE_AFTER = 4

# Most the FOCAS interval is multiplied by after repeated failures (8 x 60s = 8 minutes).
BACKOFF_MAX = 8


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _event_machine(m):
    """events.log_event wants {"id", "name"}; id is the machine name, as elsewhere."""
    return {"id": m["name"], "name": m.get("label") or m["name"]}


class Poller:
    """Keeps {machine name: state} fresh. One thread, started from create_app()."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}
        self._thread = None
        self._wake = threading.Event()

    # --- reading ------------------------------------------------------------

    def all(self):
        """A copy of every machine's state, with its age and staleness worked out now."""
        now = time.time()
        with self._lock:
            state = {k: dict(v) for k, v in self._state.items()}
        for s in state.values():
            checked = s.get("checked_at")
            s["age_seconds"] = None if checked is None else round(now - checked, 1)
            s["stale"] = self._is_stale(s)
        return state

    def get(self, name):
        return self.all().get(name, {})

    def reachable(self):
        """{machine name: True/False} for the dashboard, as system.ping_many returned."""
        return {n: s.get("reachable", False) for n, s in self.all().items()}

    @staticmethod
    def _focas_wait(entry, settings):
        """Back off after failures, so a control that is switched off is not dialled on
        every sweep -- each attempt costs the full FOCAS timeout."""
        fails = entry.get("focas_fails", 0)
        return settings["focas_interval"] * min(2 ** fails, BACKOFF_MAX)

    @staticmethod
    def _is_stale(s):
        age = s.get("age_seconds")
        return age is None or age > STALE_AFTER * max(1, s.get("interval", 30))

    # --- writing ------------------------------------------------------------

    def _update(self, name, **fields):
        with self._lock:
            entry = self._state.setdefault(name, {})
            entry.update(fields)

    def _forget(self, keep):
        with self._lock:
            for name in list(self._state):
                if name not in keep:
                    del self._state[name]

    # --- the loop -----------------------------------------------------------

    def poll_once(self, site):
        """One sweep. Separated from the loop so tests can drive it directly."""
        settings = site.get("status_poll", config.STATUS_DEFAULTS)
        machines = site["machines"]
        self._forget({m["name"] for m in machines})

        ips = [m["ip"] for m in machines if m["ip"]]
        reach = system.ping_many(ips) if ips else {}
        now = time.time()

        for m in machines:
            name = m["name"]
            was = self.get(name)
            up = reach.get(m["ip"], False) if m["ip"] else False
            self._update(name, reachable=up, checked=_now(), checked_at=now,
                         interval=settings["interval"])
            # Only log a real transition, never the first sighting: a restart should not
            # fill the log with "came online" for every machine.
            if was.get("reachable") is not None and was["reachable"] != up:
                events.log_event(_event_machine(m), "info" if up else "warning",
                                 "Reachable again" if up else "Not responding")

        if settings.get("enabled", True):
            focas = [m for m in machines if config.protocol(m) == "focas"]
            if focas:
                # Concurrently: a control that is off takes the full FOCAS timeout to fail,
                # and one of those must not hold up the others.
                with ThreadPoolExecutor(max_workers=min(8, len(focas))) as pool:
                    list(pool.map(lambda m: self._poll_focas(m, settings, now), focas))
        return self.all()

    def _poll_focas(self, m, settings, now):
        name = m["name"]
        entry = self.get(name)
        if not m.get("focas", {}).get("poll", True):
            return
        # Deliberately not gated on the ping: plenty of controls have ICMP switched off or
        # firewalled while FOCAS on 8193 answers perfectly well. A control that is actually
        # off is handled by the backoff below instead.
        last = entry.get("focas_at")
        if last is not None and now - last < self._focas_wait(entry, settings):
            return

        lock = focas_client.machine_lock(name)
        if not lock.acquire(blocking=False):
            return  # a transfer holds the control; status can wait for the next sweep
        try:
            st = focas_client.status(m)
        except focas_client.FocasError as e:
            self._update(name, focas=None, focas_error=str(e), focas_at=now, focas_checked=_now(),
                         focas_fails=entry.get("focas_fails", 0) + 1)
            return
        except Exception:
            log.exception("FOCAS poll failed for %s", name)
            return
        finally:
            lock.release()

        before = entry.get("focas") or {}
        self._update(name, focas=st, focas_error=None, focas_at=now, focas_checked=_now(),
                     focas_fails=0)
        if before and bool(before.get("alarm")) != bool(st.get("alarm")):
            events.log_event(_event_machine(m), "warning" if st.get("alarm") else "info",
                             "Alarm raised" if st.get("alarm") else "Alarm cleared")

    def _loop(self):
        while True:
            interval = config.STATUS_DEFAULTS["interval"]
            try:
                if config.exists():
                    site = config.load()
                    settings = site.get("status_poll", config.STATUS_DEFAULTS)
                    interval = settings["interval"]
                    if settings.get("enabled", True):
                        self.poll_once(site)
            except Exception:
                log.exception("Machine status poll failed")
            # Event, not sleep: a settings change can wake the loop immediately.
            self._wake.wait(interval)
            self._wake.clear()

    def refresh_soon(self):
        """Ask the loop to sweep now (after a settings or machine change)."""
        self._wake.set()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="machine-status")
            self._thread.start()


poller = Poller()
