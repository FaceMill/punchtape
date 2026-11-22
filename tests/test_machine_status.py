"""Background machine-status polling: the cache, FOCAS polling and the rules that keep it
off a busy control (per-control lock, interval, per-machine opt-out, unreachable skip),
event-log transitions, settings validation and the dashboard."""
import os
import threading
import time

from punchtape import config, focas_client, machine_status

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def rejects(site, fragment):
    try:
        config.validate(site)
    except ValueError as e:
        return fragment in str(e)
    return False


# --- fakes ------------------------------------------------------------------
calls = []
STATUS = {"mode": "MEM", "run": "START", "emergency": "", "alarm": False,
          "running": {"name": "O1234", "number": 1234}}
reachable_ips = set()


def fake_ping_many(ips):
    return {ip: ip in reachable_ips for ip in ips}


def fake_status(machine):
    calls.append(machine["name"])
    return dict(STATUS)


machine_status.system.ping_many = fake_ping_many
focas_client.status = fake_status
machine_status.focas_client = focas_client

site = config.load()
site["machines"] = [
    {"name": "fanuc1", "label": "FANUC #1", "type": "fanuc-focas", "ip": "192.168.50.21",
     "password": "x", "focas": dict(config.FOCAS_DEFAULTS)},
    {"name": "haas1", "label": "Haas #1", "type": "haas-classic", "ip": "192.168.50.11",
     "password": "x"},
]
site["status_poll"] = {"enabled": True, "interval": 5, "focas_interval": 10}
config.save(site)
site = config.load()

check(config.protocol(site["machines"][0]) == "focas", "test fixture: fanuc1 is a FOCAS machine")

# --- defaults ---------------------------------------------------------------
check(config.STATUS_DEFAULTS["interval"] == 30 and config.STATUS_DEFAULTS["focas_interval"] == 60,
      "defaults: 30s sweep, 60s FOCAS (gentle on the control's connection limit)")
check(config.FOCAS_DEFAULTS["poll"] is True, "FOCAS machines are polled by default")

# --- a sweep with everything reachable --------------------------------------
poller = machine_status.Poller()
reachable_ips = {"192.168.50.21", "192.168.50.11"}
state = poller.poll_once(site)
check(set(state) == {"fanuc1", "haas1"}, f"both machines in the cache ({sorted(state)})")
check(state["fanuc1"]["reachable"] and state["haas1"]["reachable"], "both recorded reachable")
check(state["fanuc1"]["focas"] == STATUS, "FOCAS status cached for the FANUC")
check(state["haas1"].get("focas") is None, "no FOCAS poll for a non-FOCAS machine")
check(calls == ["fanuc1"], f"exactly one FOCAS call, for the FANUC ({calls})")
check(not state["fanuc1"]["stale"], "a just-polled entry is not stale")
check(poller.reachable() == {"fanuc1": True, "haas1": True}, "reachable() maps name -> bool")

# --- the FOCAS interval is honoured -----------------------------------------
calls.clear()
poller.poll_once(site)
check(calls == [], f"a second sweep inside focas_interval does not re-ask the control ({calls})")

# --- a control that does not answer ICMP is still asked ---------------------
# Many FANUC controls have ping switched off while FOCAS answers fine, so the poll must
# not be gated on the ping.
p_noping = machine_status.Poller()
reachable_ips = set()
calls.clear()
state = p_noping.poll_once(site)
check(not state["fanuc1"]["reachable"], "no ping reply recorded")
check(calls == ["fanuc1"], f"a control that ignores ping is still polled over FOCAS ({calls})")
check(state["fanuc1"]["focas"] == STATUS, "...and its status is cached")

# --- repeated failures back off ---------------------------------------------
def always_fails(machine):
    calls.append(machine["name"])
    raise focas_client.FocasError("timed out")


focas_client.status = always_fails
p_back = machine_status.Poller()
calls.clear()
p_back.poll_once(site)
check(calls == ["fanuc1"] and p_back.get("fanuc1")["focas_fails"] == 1, "first failure counted")
calls.clear()
p_back._state["fanuc1"]["focas_at"] = time.time() - site["status_poll"]["focas_interval"] - 1
p_back.poll_once(site)
check(calls == [], "after a failure it waits longer than the normal interval before retrying")
calls.clear()
p_back._state["fanuc1"]["focas_at"] = 0          # long enough ago for any backoff
p_back.poll_once(site)
check(calls == ["fanuc1"] and p_back.get("fanuc1")["focas_fails"] == 2, "it does retry, and counts again")
focas_client.status = fake_status
calls.clear()
p_back._state["fanuc1"]["focas_at"] = 0
p_back.poll_once(site)
check(p_back.get("fanuc1")["focas_fails"] == 0, "a success resets the backoff")

reachable_ips = {"192.168.50.21", "192.168.50.11"}

# --- a transfer always wins -------------------------------------------------
reachable_ips = {"192.168.50.21", "192.168.50.11"}
poller2 = machine_status.Poller()
lock = focas_client.machine_lock("fanuc1")
held = threading.Event()
release = threading.Event()


def hold_the_control():          # stands in for a program transfer on another thread
    lock.acquire()
    held.set()
    release.wait(10)
    lock.release()


t = threading.Thread(target=hold_the_control, daemon=True)
t.start()
held.wait(5)
calls.clear()
started = time.time()
state = poller2.poll_once(site)
elapsed = time.time() - started
check(calls == [], "the poll is skipped while a transfer holds the control")
check(elapsed < 2, f"it is skipped immediately, not queued behind the transfer ({elapsed:.2f}s)")
check(state["fanuc1"]["reachable"], "reachability is still recorded while a transfer runs")
release.set()
t.join(5)

# the lock is free again, so the next sweep polls
calls.clear()
check(poller2.poll_once(site)["fanuc1"]["focas"] == STATUS and calls == ["fanuc1"],
      "the control is polled once the transfer finishes")

# --- the lock is reentrant on one thread ------------------------------------
lock = focas_client.machine_lock("fanuc1")
check(lock.acquire(blocking=False) and lock.acquire(blocking=False),
      "the per-control lock is reentrant (the poller holds it, then status() takes it again)")
lock.release()
lock.release()

# --- per-machine opt-out ----------------------------------------------------
off = config.load()
off["machines"] = [dict(m) for m in site["machines"]]
off["machines"][0]["focas"] = {**config.FOCAS_DEFAULTS, "poll": False}
off["status_poll"] = dict(site["status_poll"])
calls.clear()
state = machine_status.Poller().poll_once(off)
check(calls == [], "a control with polling switched off is not asked")
check(state["fanuc1"]["reachable"], "...but it is still pinged, so the dashboard shows it")
check(state["fanuc1"].get("focas") is None, "...and holds no FOCAS status")

# --- polling disabled altogether --------------------------------------------
disabled = config.load()
disabled["machines"] = site["machines"]
disabled["status_poll"] = {"enabled": False, "interval": 5, "focas_interval": 10}
calls.clear()
state = machine_status.Poller().poll_once(disabled)
check(calls == [], "disabled: no FOCAS calls")
check(set(state) == {"fanuc1", "haas1"}, "disabled: a direct sweep still answers (used as a fallback)")

# --- a FOCAS error is cached, not raised ------------------------------------
def boom(machine):
    raise focas_client.FocasError("connection refused")


focas_client.status = boom
p3 = machine_status.Poller()
state = p3.poll_once(site)
check(state["fanuc1"]["focas"] is None and "refused" in (state["fanuc1"]["focas_error"] or ""),
      f"a control that errors is recorded, not raised ({state['fanuc1'].get('focas_error')})")
focas_client.status = fake_status

# --- transitions reach the event log ----------------------------------------
from punchtape import events  # noqa: E402

p4 = machine_status.Poller()
reachable_ips = {"192.168.50.21", "192.168.50.11"}
p4.poll_once(site)                      # first sighting
before = len(events.recent_events(200))
p4.poll_once(site)
check(len(events.recent_events(200)) == before, "a first sighting does not log 'came online'")

reachable_ips = set()
p4.poll_once(site)
msgs = [e["message"] for e in events.recent_events(20)]
check(any("Not responding" in m for m in msgs), f"going offline is logged ({msgs[:2]})")
reachable_ips = {"192.168.50.21", "192.168.50.11"}
p4.poll_once(site)
msgs = [e["message"] for e in events.recent_events(20)]
check(any("Reachable again" in m for m in msgs), "coming back is logged")

# alarm transition
p5 = machine_status.Poller()
p5.poll_once(site)
focas_client.status = lambda m: {**STATUS, "alarm": True}
time.sleep(0.01)
p5._state["fanuc1"]["focas_at"] = 0      # force the interval to have elapsed
p5.poll_once(site)
msgs = [e["message"] for e in events.recent_events(20)]
check(any("Alarm raised" in m for m in msgs), f"an alarm appearing is logged ({msgs[:2]})")
focas_client.status = fake_status

# --- staleness --------------------------------------------------------------
p6 = machine_status.Poller()
p6.poll_once(site)
p6._state["fanuc1"]["checked_at"] = time.time() - 10_000
check(p6.all()["fanuc1"]["stale"], "an old entry is reported stale")
check(p6.all()["fanuc1"]["age_seconds"] > 9000, "age is worked out when read, not when written")

# --- machines that go away --------------------------------------------------
p7 = machine_status.Poller()
p7.poll_once(site)
fewer = config.load()
fewer["machines"] = [site["machines"][1]]
fewer["status_poll"] = dict(site["status_poll"])
check(set(p7.poll_once(fewer)) == {"haas1"}, "a removed machine is dropped from the cache")

# --- settings validation ----------------------------------------------------
base = config.load()


def with_poll(**kw):
    return {**base, "status_poll": {**config.STATUS_DEFAULTS, **kw}}


check(rejects(with_poll(interval=1), "5 to 3600"), "rejects too short a sweep interval")
check(rejects(with_poll(interval=99999), "5 to 3600"), "rejects too long a sweep interval")
check(rejects(with_poll(focas_interval=2), "10 to 3600"), "rejects too short a FOCAS interval")
check(rejects(with_poll(interval=60, focas_interval=30), "cannot be shorter"),
      "rejects a FOCAS interval shorter than the sweep (it could never be honoured)")
check(rejects(with_poll(interval="soon"), "whole number"), "rejects a non-numeric interval")
check(rejects(with_poll(interval=10.5), "whole number"), "rejects a fractional interval")
ok = with_poll(interval=30, focas_interval=60)
config.validate(ok)
check(ok["status_poll"]["enabled"] is True, "enabled is coerced to a bool")

# --- old configs ------------------------------------------------------------
old = {k: v for k, v in config.load().items() if k != "status_poll"}
config.save(old)
check(config.load()["status_poll"] == config.STATUS_DEFAULTS,
      "a config written before polling existed gets the defaults")

# --- the dashboard renders the cache ----------------------------------------
from werkzeug.security import generate_password_hash  # noqa: E402

from punchtape import create_app  # noqa: E402

config.set_admin_password_hash(generate_password_hash("dash-pass-1"))
restore = config.load()
restore["machines"] = site["machines"]
config.save(restore)
app = create_app()
c = app.test_client()
c.post("/login", data={"username": "admin", "password": "dash-pass-1",
                       "csrf": c.get("/login").text.split('name="csrf" value="')[1].split('"')[0]},
       follow_redirects=True)
machine_status.poller._state = {
    "fanuc1": {"reachable": True, "checked_at": time.time(), "interval": 30, "focas": dict(STATUS)},
    "haas1": {"reachable": False, "checked_at": time.time(), "interval": 30},
}
body = c.get("/").text
check("O1234" in body, "dashboard shows the FOCAS machine's active program")
check("start" in body.lower() and "MEM" in body, "dashboard shows run state and mode")
check("no reply" in body, "dashboard shows an unreachable machine")

api = c.get("/api/machine-status")
check(api.status_code == 200 and api.get_json()["fanuc1"]["focas"]["running"]["name"] == "O1234",
      "the JSON endpoint returns the same state")

print("ALL PASSED" if not fails else f"{len(fails)} FAILED", flush=True)
os._exit(1 if fails else 0)
