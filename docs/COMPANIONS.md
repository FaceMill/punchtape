# Running another app on the same Pi

PunchTape does one job: it moves part programs between the office and the machines, and
keeps the machines' old protocols off the office network. It deliberately does not do
machine monitoring — no OEE, no spindle-load charts, no uptime reports.

Those tools exist, and the PunchTape Pi is the natural place to run one: it is already the
only box with a leg on both networks. This page is how to do that without breaking either
PunchTape's security model or the other app.

A **companion app** here means an app that:

* runs in Docker on the same Pi,
* office PCs reach on one TCP port, and
* PunchTape does not start, stop, update or back up.

PunchTape manages exactly one thing for it: the firewall hole on the office side.

## Opening the port

The generated firewall (`generated/nftables.conf`) is rewritten from `config/site.json`
every time anything changes, so hand-editing it does not survive. Register the app
instead:

```bash
sudo docker exec punchtape-admin python -m punchtape.cli \
     companion --add chatter --port 3001 --note "machine monitor"
```

That adds one rule, on the office interface only, restricted to the same allowed office
subnets as the admin page:

```
iifname "eth0" ip saddr { 10.20.0.0/16 } tcp dport 3001 accept  # chatter
```

Other commands:

```bash
... companion --list
... companion --remove chatter
... companion --add chatter --port 3005      # re-adding a name moves its port
```

Ports PunchTape itself uses are refused with a message saying which service owns them
(21, 22, 80, 139, 443, 445, 8090, 8095, 8443, 9000, 9090, 9443 and 30000–30049).

What this does **not** do:

* It does not open anything on the CNC side. Machines still reach only their own service
  and their own folder.
* It does not route between the two networks. `net.ipv4.ip_forward` stays 0 and the
  forward chain stays `policy drop`.
* It does not touch the output chain, which is already `policy accept` — so a companion
  can poll machines on the CNC network and reach the internet without any change.

## The four things that will bite you

These apply to any companion app, not just Chatter.

### 1. Docker's own firewalling is off

`host/install.sh` writes `"iptables": false` and `"ip6tables": false` into
`/etc/docker/daemon.json`, because the host nftables ruleset is meant to be the only
firewall on the Pi. The consequence is bigger than it sounds:

* no NAT, so a bridge-networked container cannot reach anything off its bridge —
  not the machines, not the office, not the internet;
* `ports:` publishes nothing;
* with `ip_forward=0` and a dropping forward chain, even enabling Docker's iptables
  would not restore routing.

**So a companion app must use `network_mode: host`.** Nearly every third-party compose
file assumes bridge networking, which is why they need adapting rather than just running.

### 2. Host networking means no compose DNS

Multi-container stacks address each other by service name (`http://victoriametrics:8428`).
Under host networking there is no compose network and no such DNS. Every internal URL
becomes `127.0.0.1:<port>`.

### 3. Host networking exposes every listener on *both* interfaces

On a bridge, binding `0.0.0.0` only exposed the container. On the host network it means
the office NIC, the CNC NIC and loopback. Anything without a login must be given an
explicit loopback bind — a database or alerting sidecar should never be listening on the
CNC switch, even though the firewall would drop the packets.

The app's own front end can bind `0.0.0.0` safely: the input chain is default-drop, so
only the office subnets you registered reach it.

### 4. FOCAS connections are a shared, limited resource

This is the one real *functional* conflict, and no amount of firewall configuration fixes it.

PunchTape's `focas-bridge` opens TCP 8193 to a FANUC for program transfer. A monitoring
app polls the same port on the same control, continuously. FANUC controls allow only a
small number of simultaneous FOCAS connections, and the limit is per control, not per
client. Exceed it and connections fail — on either side, and intermittently.

If you run both against the same FANUC:

* Give the monitor the slowest poll interval you can live with.
* Expect `Send to CNC` / `Read from CNC` to occasionally need a retry while a poll holds a
  handle.
* If the control is a 16i/18i/21i, be more careful still; the older Ethernet boards have
  the smallest connection limits.

Machines on SMB, FTP or RS-232 have no equivalent problem — PunchTape is the server there,
and a monitor that uses a different protocol (MTConnect, Haas MDC on 9998/5051) does not
contend with it at all.

## Chatter specifically

[Chatter](https://github.com/toolpath/chatter) is a good fit: it is read-only telemetry
plus file transfer *to* machines' own FTP servers, where PunchTape is a file server *for*
machines. No port collides — Chatter wants 3001, 3002, 8428 and 8880, none of which
PunchTape uses.

Note what it cannot cover, so you know what PunchTape is still doing: Chatter has no SMB
of any version and no serial DNC, so it cannot talk to a Haas Classic over a network
share, a Siemens 840D PCU 50, or anything on RS-232. Its Haas support is the Ethernet
machine-data option (ports 9998/5051), not the file share.

A corrected compose file is in [`companions/chatter.yml`](../companions/chatter.yml).

```bash
cd /opt/punchtape/companions

# vmalert reads a rules file that ships in the Chatter repo.
git clone --depth 1 https://github.com/toolpath/chatter chatter-src

docker compose -f chatter.yml up -d

sudo docker exec punchtape-admin python -m punchtape.cli \
     companion --add chatter --port 3001 --note "machine monitor"
```

Then open `http://<pi>:3001` from the office and create the Chatter admin login.

For HTTPS, either follow Chatter's own reverse-proxy guide, or point the Pi's existing
Caddy at it by adding a block to `caddy/Caddyfile` and restarting Caddy:

```
https://{$OFFICE_HOSTNAME}:8444, https://localhost:8444, https://:8444 {
	tls internal
	reverse_proxy 127.0.0.1:3001
}
```

With that in place register the companion on **8444** instead of 3001, and Chatter is
reachable only over TLS with the same internal CA as the admin page.

## Backups

`host/backup.sh` backs up PunchTape's own data and nothing else — a companion's Docker
volumes are not included. Back them up separately, or add their volume paths to your own
restic invocation. Chatter's live in `chatterbox-data` and `victoriametrics-data`.

## Updating

`host/update.sh` only rebuilds PunchTape's containers. A companion is updated on its own
schedule, with its own compose file. Nothing in PunchTape's update path will restart,
reconfigure or remove it — and the companion firewall rule survives, because it lives in
`config/site.json` and is re-rendered like everything else.
