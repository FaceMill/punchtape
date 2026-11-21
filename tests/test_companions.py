"""Companion apps (docs/COMPANIONS.md): another app on the same Pi gets one office-side
port in the generated firewall. Covers the CLI, the rendered ruleset checked with the real
nft, validation (reserved and duplicate ports, bad names), and that the CNC side and the
forward chain are untouched."""
import os
import subprocess

from punchtape import config

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def cli(*args):
    return subprocess.run(["python", "-m", "punchtape.cli", *args], capture_output=True, text=True)


def generated(name):
    return open(f"/app/generated/{name}").read()


def nft_ok():
    return subprocess.run(["nft", "-c", "-f", "/app/generated/nftables.conf"], capture_output=True).returncode == 0


def rejects(site, fragment):
    """validate() refuses this config, with a message mentioning `fragment`."""
    try:
        config.validate(site)
    except ValueError as e:
        return fragment in str(e)
    return False


office_if = config.load()["office"]["interface"]
subnets = ", ".join(config.load()["office"]["allowed_subnets"])

# --- nothing registered -----------------------------------------------------
check("# chatter" not in generated("nftables.conf"), "no companions: no companion rules")

# --- add --------------------------------------------------------------------
r = cli("companion", "--add", "chatter", "--port", "3001", "--note", "machine monitor")
check(r.returncode == 0, f"companion --add succeeded ({r.stdout.strip()[-60:]}{r.stderr.strip()[-60:]})")
site = config.load()
check(site["companions"] == [{"name": "chatter", "port": 3001, "note": "machine monitor"}],
      f"recorded in site.json ({site['companions']})")

fw = generated("nftables.conf")
want = f'iifname "{office_if}" ip saddr {{ {subnets} }} tcp dport 3001 accept  # chatter'
check(want in fw, f"firewall: office-side accept for tcp/3001 ({want!r})")
check(nft_ok(), "ruleset with a companion passes the real nft -c")

# The whole point is that it does not widen anything else.
check(f'iifname "{office_if}" ip saddr {{ {subnets} }} tcp dport {{ 22, 80, 443, 445, 8443, 9090 }} accept' in fw,
      "firewall: PunchTape's own office port list is unchanged")
check("type filter hook forward priority 0; policy drop;" in fw,
      "firewall: still nothing routed between office and CNC")
cnc_if = config.load()["cnc"]["interface"]
check(not any("3001" in ln and cnc_if in ln for ln in fw.splitlines()),
      "firewall: nothing opened on the CNC interface")

# --- list, re-add (move the port), remove ------------------------------------
r = cli("companion", "--list")
check("chatter" in r.stdout and "3001" in r.stdout, f"companion --list shows it ({r.stdout.strip()})")

r = cli("companion", "--add", "chatter", "--port", "3005")
site = config.load()
check(r.returncode == 0 and [c["port"] for c in site["companions"]] == [3005],
      f"re-adding a name moves its port, no duplicate ({site['companions']})")
check("tcp dport 3005 accept" in generated("nftables.conf")
      and "tcp dport 3001 accept" not in generated("nftables.conf"),
      "firewall followed the port change")

r = cli("companion", "--remove", "nosuch")
check(r.returncode != 0, "removing an unknown companion fails")

r = cli("companion", "--remove", "chatter")
check(r.returncode == 0 and config.load()["companions"] == [], "companion --remove clears it")
check("# chatter" not in generated("nftables.conf"), "firewall rule withdrawn")
check(nft_ok(), "ruleset without companions still passes nft -c")

# --- validation -------------------------------------------------------------
base = config.load()


def with_companions(*entries):
    return {**base, "companions": [dict(e) for e in entries]}


check(rejects(with_companions({"name": "x", "port": 8443}), "8443"),
      "rejects a port PunchTape serves (8443, the admin page)")
check(rejects(with_companions({"name": "x", "port": 30010}), "passive"),
      "rejects a port inside the FTP passive range")
check(rejects(with_companions({"name": "x", "port": 22}), "SSH"),
      "rejects SSH, naming the service")
check(rejects(with_companions({"name": "a", "port": 3001}, {"name": "b", "port": 3001}), "already used"),
      "rejects two companions on one port")
check(rejects(with_companions({"name": "a", "port": 3001}, {"name": "a", "port": 3002}), "twice"),
      "rejects a duplicate name")
check(rejects(with_companions({"name": "Bad Name", "port": 3001}), "lowercase"),
      "rejects a name that is not a safe token")
check(rejects(with_companions({"name": "x", "port": 70000}), "1 to 65535"),
      "rejects an out-of-range port")
check(rejects(with_companions({"name": "x", "port": 3001.5}), "whole number"),
      "rejects a fractional port")
check(rejects(with_companions({"name": "x", "port": True}), "whole number"),
      "rejects a boolean port")
check(rejects(with_companions(*[{"name": f"c{i}", "port": 3000 + i} for i in range(9)]), "at most"),
      "rejects more than the maximum number of companions")

# A name with a shell/nft metacharacter must never reach the ruleset.
check(rejects(with_companions({"name": "a; drop", "port": 3001}), "lowercase"),
      "rejects a name containing nft syntax")

# --- configs written before this setting existed ----------------------------
old = {k: v for k, v in base.items() if k != "companions"}
config.save(old)
check(config.load()["companions"] == [], "a config with no companions key loads as empty")
check(nft_ok(), "ruleset from a pre-companion config still passes nft -c")

print("ALL PASSED" if not fails else f"{len(fails)} FAILED", flush=True)
os._exit(1 if fails else 0)
