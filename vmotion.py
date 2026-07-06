#!/usr/bin/env python3
"""
vMotion emulator for SR Linux dynamic sub-interfaces.

Mimics a VMware vSphere vMotion: a "VM" (a fixed MAC + IP on a given VLAN) moves
from a source client/leaf-port to a target client/leaf-port, and — exactly like
ESXi — announces its new location with a gratuitous RARP burst (see rarp_agent.py).

The interesting twist on SR Linux: with dynamic sub-interfaces the VLAN may not
exist on the *target* leaf yet. The RARP is what a real vMotion relies on to move
the MAC, but here that same first tagged frame is consumed as the active-VLAN
*trigger* and dropped before the sub-interface / MAC-VRF / VXLAN are provisioned.
This tool measures whether the RARP is sufficient by timing, relative to the
cutover (t0):
  - when the target leaf's sub-interface for the VLAN comes oper-up, and
  - when the VM MAC becomes *local* on the target leaf (destination-type
    sub-interface) — i.e. when EVPN can actually advertise the new location.
It also counts how many RARPs were sent *before* the sub-interface existed (those
a real single-shot vMotion would have lost), and optionally measures the dataplane
outage seen by a stationary peer that keeps pinging the VM across the move.

Topology assumptions (this lab): single-homed sh-clientN ->
  N in 1..10  -> leaf1  ethernet-1/N
  N in 11..20 -> leaf3  ethernet-1/(N-10)
Leaf gNMI mgmt IPs are derived from the container name via docker inspect.

Example (cross-leaf move, leaf1 -> leaf3, stationary peer on leaf1):
  ./vmotion.py --vlan 1055 --src sh-client1 --dst sh-client11 --peer sh-client5
  ./vmotion.py --vlan 1056 --src sh-client1 --dst sh-client11 --rarp-mode once   # show insufficiency
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

USER = "admin"
PASSWORD = "NokiaSrl1!"
GNMI_PORT = 57400


# ----------------------------------------------------------------------------- helpers

def sh(cmd, timeout=60, text_input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=text_input)
    return r.returncode, r.stdout, r.stderr


def docker_mgmt_ip(node):
    code, out, _ = sh(["docker", "inspect", "-f",
                       "{{range .NetworkSettings.Networks}}{{.IPAddress}}\n{{end}}", node])
    if code != 0:
        return None
    for line in out.splitlines():
        ip = line.strip()
        if ip:
            return ip
    return None


def gnmic_get(mgmt, paths, datatype="state"):
    cmd = ["gnmic", "-a", f"{mgmt}:{GNMI_PORT}", "-u", USER, "-p", PASSWORD, "--skip-verify",
           "-e", "json_ietf", "get", "--type", datatype]
    for p in paths:
        cmd += ["--path", p]
    code, out, _ = sh(cmd)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _first_value(data):
    """Return the first scalar value from a single-leaf gNMI get response."""
    if not data:
        return None
    for src in data:
        for upd in src.get("updates", []):
            for _k, v in upd.get("values", {}).items():
                return v
    return None


def _mac_list(data):
    """Return the list of mac-table entries from a bridge-table mac-table get."""
    entries = []
    if not data:
        return entries
    for src in data:
        for upd in src.get("updates", []):
            for _k, v in upd.get("values", {}).items():
                if isinstance(v, dict):
                    lst = v.get("mac") or v.get("srl_nokia-bridge-table-mac-table:mac") or []
                    if isinstance(lst, dict):
                        lst = [lst]
                    entries.extend(lst)
    return entries


def subif_oper(mgmt, port, vlan):
    v = _first_value(gnmic_get(mgmt, [f"/interface[name={port}]/subinterface[index={vlan}]/oper-state"]))
    return v if isinstance(v, str) else None


def mac_entry(mgmt, vlan, mac):
    """Return the mac-table entry dict for `mac` in network-instance VLAN-<vlan>, or None."""
    ni = f"VLAN-{vlan}"
    data = gnmic_get(mgmt, [f"/network-instance[name={ni}]/bridge-table/mac-table"])
    target = mac.upper()
    for e in _mac_list(data):
        if str(e.get("address", "")).upper() == target:
            return e
    return None


def client_location(container):
    """Map a single-homed client container name to (leaf_container, port, client_id)."""
    m = re.search(r"(\d+)$", container)
    if not m:
        raise SystemExit(f"cannot parse client id from '{container}'")
    n = int(m.group(1))
    if 1 <= n <= 10:
        return "leaf1", f"ethernet-1/{n}", n
    if 11 <= n <= 20:
        return "leaf3", f"ethernet-1/{n - 10}", n
    raise SystemExit(f"only sh-client1..20 are supported (got '{container}')")


def default_vm_mac(vlan):
    # VMware OUI 00:50:56, VLAN encoded in the low bytes for readability
    return f"00:50:56:{(vlan >> 8) & 0xff:02x}:{vlan & 0xff:02x}:01"


def vm_ip(vlan, host=200):
    return f"10.{vlan // 100}.{vlan % 100}.{host}"


def peer_ip(vlan, host_id):
    return f"10.{vlan // 100}.{vlan % 100}.{host_id}"


# ----------------------------------------------------------------------------- client ops

def create_subif(client, parent, vlan, mac, ip):
    vif = f"{parent}.{vlan}"
    script = "\n".join([
        f"ip link add link {parent} name {vif} type vlan id {vlan} 2>/dev/null",
        f"echo 1 > /proc/sys/net/ipv6/conf/{vif}/disable_ipv6 2>/dev/null || true",
        f"ip link set dev {vif} address {mac}",
        f"ip link set dev {vif} up",
        f"ip addr add {ip}/24 dev {vif} 2>/dev/null || true",
        "",
    ])
    sh(["docker", "exec", "-i", client, "sh"], text_input=script)


def delete_subif(client, parent, vlan):
    sh(["docker", "exec", client, "ip", "link", "del", f"{parent}.{vlan}"])


def flush_source_leaf_subif(leaf, port, vlan):
    """Delete the source leaf's dynamic sub-interface + NI binding, emulating the
    source vNIC going link-down at switchover. This flushes the stale local MAC so
    EVPN mobility can complete; the dynamic sub-interface would otherwise persist
    (and keep the MAC local) until the retention timer expires."""
    ni = f"VLAN-{vlan}"
    script = "\n".join([
        "enter candidate",
        f"delete network-instance {ni} interface {port}.{vlan}",
        f"delete interface {port} subinterface {vlan}",
        "commit stay",
        "quit",
        "",
    ])
    sh(["docker", "exec", "-i", leaf, "sr_cli"], text_input=script)


def push_rarp_agent(client):
    agent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rarp_agent.py")
    sh(["docker", "cp", agent, f"{client}:/tmp/rarp_agent.py"])


def send_rarp(client, vif, mac, count=1, interval=0.0):
    sh(["docker", "exec", client, "python3", "/tmp/rarp_agent.py", vif, mac,
        str(count), str(interval)], timeout=30)


def dummy_ip(vlan):
    """An unused same-subnet address the VM can ARP for (never answered)."""
    return f"10.{vlan // 100}.{vlan % 100}.254"


def send_nudge(client, vif, dst_ip):
    """Emit a real data frame from the VM — a ping to a dummy same-subnet host, i.e. a
    tagged ARP request sourced from the VM MAC. On this SR Linux build a lone/repeated
    RARP does not reliably latch active-VLAN detection nor get the MAC learnt local; a
    genuine data frame does. This represents the resumed VM sourcing traffic right after
    the move (the RARP is the announcement; the VM then sends normal frames)."""
    sh(["docker", "exec", client, "ping", "-n", "-c", "1", "-W", "1", "-I", vif, dst_ip],
       timeout=5)


def start_peer_ping(peer, dst_ip, count, interval):
    # -D prints unix timestamps so we can align losses to the cutover (t0)
    sh(["docker", "exec", "-d", peer, "sh", "-c",
        f"ping -n -D -i {interval} -c {count} {dst_ip} > /tmp/vmotion_ping.txt 2>&1"])


def collect_peer_ping(peer):
    _c, out, _e = sh(["docker", "exec", peer, "cat", "/tmp/vmotion_ping.txt"])
    ts = []
    for line in out.splitlines():
        m = re.search(r"\[(\d+\.\d+)\].*icmp_seq=(\d+)", line)
        if m:
            ts.append(float(m.group(1)))
    return sorted(ts)


# ----------------------------------------------------------------------------- warm / poll

def announce_until_local(client, vif, mac, mgmt, vlan, timeout, interval=0.2, nudge=True):
    """Keep announcing until `mac` is learnt local on `mgmt` (or timeout).
    Returns (elapsed_or_None, rarps_sent). Each round sends the RARP (the vMotion
    announcement) plus, unless nudge=False, a data-frame nudge that reliably triggers
    active-VLAN detection and gets the MAC learnt on this build (see send_nudge)."""
    t0 = time.time()
    sent = 0
    while time.time() - t0 < timeout:
        send_rarp(client, vif, mac, count=1)
        if nudge:
            send_nudge(client, vif, dummy_ip(vlan))
        sent += 1
        e = mac_entry(mgmt, vlan, mac)
        if e and e.get("destination-type") == "sub-interface":
            return time.time() - t0, sent
        time.sleep(interval)
    return None, sent


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Emulate VMware vMotion on SR Linux dynamic sub-interfaces.")
    ap.add_argument("--vlan", type=int, required=True, help="VLAN the VM lives on (must be in the leaf's dynamic vlan-range)")
    ap.add_argument("--src", required=True, help="source client container (VM starts here), e.g. sh-client1")
    ap.add_argument("--dst", required=True, help="target client container (VM moves here), e.g. sh-client11")
    ap.add_argument("--peer", default=None, help="stationary client that pings the VM across the move (outage measurement)")
    ap.add_argument("--vm-mac", default=None, help="VM MAC (default derived from VLAN, VMware OUI)")
    ap.add_argument("--vm-ip", default=None, help="VM IP (default 10.<vh>.<vl>.200)")
    ap.add_argument("--rarp-mode", choices=["once", "burst", "sustained"], default="sustained",
                    help="once: 1 RARP at cutover; burst: --rarp-count RARPs; "
                         "sustained: keep announcing until the MAC moves (default)")
    ap.add_argument("--rarp-count", type=int, default=5, help="RARPs for --rarp-mode burst (default 5)")
    ap.add_argument("--rarp-interval", type=float, default=0.2, help="seconds between RARPs in sustained/burst (default 0.2)")
    ap.add_argument("--timeout", type=float, default=20.0, help="give up waiting for MAC move after this many s")
    ap.add_argument("--poll", type=float, default=0.3, help="leaf gNMI poll interval (s)")
    ap.add_argument("--warm-timeout", type=float, default=20.0, help="max wait for the source VM to be learnt before moving")
    ap.add_argument("--flush-source-leaf", action="store_true",
                    help="at cutover, also delete the SOURCE leaf's dynamic sub-interface, "
                         "emulating the source vNIC going link-down (which flushes the stale "
                         "local MAC and lets EVPN mobility complete). Without this, the source "
                         "leaf keeps the VM MAC local until aging/retention and peers black-hole.")
    ap.add_argument("--no-nudge", action="store_true",
                    help="RARP only — disable the data-frame nudge (send_nudge) during "
                         "source warm-up and cutover. On this SR Linux build a lone RARP "
                         "does not reliably trigger detection / get learnt / win mobility, "
                         "so expect warm-up and/or mobility to fail or be slow with this set.")
    ap.add_argument("--no-source-setup", action="store_true", help="assume the VM already exists on --src")
    ap.add_argument("--keep", action="store_true", help="do not delete the VM/peer sub-interfaces at the end")
    ap.add_argument("--json-report", default=None)
    args = ap.parse_args()

    vlan = args.vlan
    mac = args.vm_mac or default_vm_mac(vlan)
    ip = args.vm_ip or vm_ip(vlan)
    parent = "eth1"

    src_leaf, src_port, _ = client_location(args.src)
    dst_leaf, dst_port, _ = client_location(args.dst)
    src_mgmt = docker_mgmt_ip(src_leaf)
    dst_mgmt = docker_mgmt_ip(dst_leaf)
    if not src_mgmt or not dst_mgmt:
        print(f"ERROR: could not resolve leaf mgmt IPs ({src_leaf}={src_mgmt}, {dst_leaf}={dst_mgmt})", file=sys.stderr)
        sys.exit(1)

    print("=" * 74)
    print("vMotion emulation — SR Linux dynamic sub-interfaces")
    print("=" * 74)
    print(f"VM            : mac={mac}  ip={ip}  vlan={vlan}")
    print(f"Source        : {args.src}  ->  {src_leaf} {src_port} (mgmt {src_mgmt})")
    print(f"Target        : {args.dst}  ->  {dst_leaf} {dst_port} (mgmt {dst_mgmt})")
    if args.peer:
        peer_leaf, peer_port, peer_id = client_location(args.peer)
        pip = peer_ip(vlan, peer_id)
        print(f"Peer (static) : {args.peer}  ->  {peer_leaf} {peer_port}  ip={pip}")
    print(f"RARP mode     : {args.rarp_mode}"
          + (f" (count={args.rarp_count})" if args.rarp_mode == "burst" else "")
          + (f" (interval={args.rarp_interval}s)" if args.rarp_mode != "once" else ""))
    print("=" * 74)

    push_rarp_agent(args.dst)

    # Warn if the target VLAN is already warm on the target leaf (not a true
    # "VLAN doesn't exist yet" demonstration).
    if subif_oper(dst_mgmt, dst_port, vlan) is not None:
        print(f"WARNING: subinterface {dst_port}.{vlan} already exists on {dst_leaf} "
              f"(target not cold) — dynamic creation will not be exercised.", file=sys.stderr)

    # ---- Phase A: place + warm the VM on the source ------------------------------------
    if not args.no_source_setup:
        push_rarp_agent(args.src)
        print(f"[setup] Placing VM on source {args.src} ({src_leaf} {src_port})...")
        create_subif(args.src, parent, vlan, mac, ip)
        # Announce until the source leaf learns the MAC locally. Bringing the subif
        # up is the active-VLAN trigger; the RARP is the frame that gets it learnt,
        # but the first RARPs are dropped during provisioning, so we sustain here.
        el, warm_rarps = announce_until_local(args.src, f"{parent}.{vlan}", mac,
                                              src_mgmt, vlan, args.warm_timeout, args.rarp_interval,
                                              nudge=not args.no_nudge)
        if el is None:
            print(f"ERROR: VM MAC never became local on {src_leaf} within {args.warm_timeout}s; "
                  f"aborting.", file=sys.stderr)
            sys.exit(1)
        print(f"[setup] VM learnt locally on {src_leaf} after {el:.2f}s ({warm_rarps} RARPs).")

    # ---- Phase B: place + warm the stationary peer -------------------------------------
    if args.peer:
        peer_leaf, peer_port, peer_id = client_location(args.peer)
        pip = peer_ip(vlan, peer_id)
        print(f"[setup] Placing peer {args.peer} ({peer_leaf} {peer_port})...")
        create_subif(args.peer, parent, vlan, peer_id_to_mac(peer_id, vlan), pip)
        # Warm the peer's path to the VM (resolve ARP) so the pre-move baseline is clean.
        sh(["docker", "exec", args.peer, "ping", "-n", "-c", "3", "-i", "0.2", "-w", "5", ip])
        pe = mac_entry(peer_leaf and docker_mgmt_ip(peer_leaf), vlan, mac)
        print(f"[setup] Peer path warm (VM seen on peer leaf as: "
              f"{pe.get('destination-type') if pe else 'unknown'}).")

    # ---- Phase C: start the peer's continuous ping across the move ---------------------
    ping_interval = 0.1
    if args.peer:
        window = args.warm_timeout + args.timeout + 5.0
        cnt = int(window / ping_interval)
        start_peer_ping(args.peer, ip, cnt, ping_interval)
        time.sleep(1.0)  # baseline samples before cutover

    # ---- Phase D: cutover --------------------------------------------------------------
    print("-" * 74)
    print(f"[cutover] Moving VM {mac} : {args.src} ({src_leaf}) -> {args.dst} ({dst_leaf})")
    t0 = time.time()
    delete_subif(args.src, parent, vlan)          # VM leaves the source host
    if args.flush_source_leaf:
        flush_source_leaf_subif(src_leaf, src_port, vlan)  # emulate source vNIC link-down
    create_subif(args.dst, parent, vlan, mac, ip)  # VM appears on target (triggers detection)
    vif = f"{parent}.{vlan}"

    initial = 0
    if args.rarp_mode == "once":
        send_rarp(args.dst, vif, mac, count=1)
        initial = 1
    elif args.rarp_mode == "burst":
        send_rarp(args.dst, vif, mac, count=args.rarp_count, interval=args.rarp_interval)
        initial = args.rarp_count
    print(f"[cutover] VM re-created on target; RARP announcement started (mode={args.rarp_mode}).")

    # One loop tracks target provisioning (subif up, MAC learnt local) AND source-leaf
    # mobility (source stops claiming the VM local) — while the moved VM keeps announcing
    # the WHOLE time. This models a real migrated VM: it resumes and keeps sending, and
    # that continuous traffic is what drives EVPN mobility to converge. If we instead stop
    # at the first target-learn (as an earlier version did), the moved VM goes silent, its
    # target-local entry is not reinforced, and the source leaf's stale (still-aging) local
    # entry wins the mobility arbitration — so the target yields to remote and the move
    # never propagates (both leaves point back to the source). Keep talking until the
    # source leaf releases the MAC (mobility_t) or we time out.
    subif_up_t = None
    mac_local_t = None
    mobility_t = None
    rarps = initial
    rarps_before_up = None
    deadline = t0 + args.timeout
    while time.time() < deadline:
        if args.rarp_mode == "sustained" and mobility_t is None:
            send_rarp(args.dst, vif, mac, count=1)
            if not args.no_nudge:
                send_nudge(args.dst, vif, dummy_ip(vlan))  # resumed-VM traffic — drives mobility
            rarps += 1

        if subif_up_t is None and subif_oper(dst_mgmt, dst_port, vlan) == "up":
            subif_up_t = time.time() - t0
            rarps_before_up = rarps

        if mac_local_t is None:
            e = mac_entry(dst_mgmt, vlan, mac)
            if e and e.get("destination-type") == "sub-interface":
                mac_local_t = time.time() - t0

        # Source-leaf mobility can only complete once the target holds the MAC local.
        if mac_local_t is not None and mobility_t is None:
            se = mac_entry(src_mgmt, vlan, mac)
            if se is None or se.get("destination-type") != "sub-interface":
                mobility_t = time.time() - t0
                break

        time.sleep(args.poll if args.rarp_mode != "sustained" else max(0.0, args.rarp_interval - 0.05))

    # ---- Phase E: settle + collect the peer outage -------------------------------------
    src_after = mac_entry(src_mgmt, vlan, mac)
    outage_ms = None
    recovery_ms = None
    if args.peer:
        # Let the dataplane settle so post-convergence replies are actually captured
        # (control-plane mobility completing does not instantly mean the first ICMP
        # reply has been seen), then stop the pinger and parse.
        time.sleep(3.0)
        sh(["docker", "exec", args.peer, "pkill", "-INT", "-f", "ping -n -D"])
        ts = collect_peer_ping(args.peer)
        if len(ts) >= 2:
            gaps = [(ts[i + 1] - ts[i]) for i in range(len(ts) - 1)]
            outage_ms = max(gaps) * 1000.0
            after = [t for t in ts if t > t0]
            if after:
                recovery_ms = (min(after) - t0) * 1000.0
            else:
                recovery_ms = None  # never recovered within the window

    # ---- Report ------------------------------------------------------------------------
    print("=" * 74)
    print("RESULT")
    print("=" * 74)
    print(f"Target subif up (t0+)     : {fmt(subif_up_t)} s")
    print(f"VM MAC local on target    : {fmt(mac_local_t)} s")
    print(f"RARPs sent (total)        : {rarps}")
    if rarps_before_up is not None:
        print(f"RARPs before subif existed: {rarps_before_up}  (dropped — trigger-only, not forwardable)")
    print(f"MAC mobility on src leaf  : "
          + (f"released at t0+{mobility_t:.2f}s" if mobility_t is not None
             else "NOT released (source leaf still claims VM local)"))
    print(f"Source leaf MAC after move: "
          + (f"{src_after.get('type')} / {src_after.get('destination-type')} "
             f"({src_after.get('destination')})" if src_after else "absent"))
    if args.peer:
        print(f"Peer max outage           : {fmt_ms(outage_ms)}")
        print(f"Peer recovery (t0+)       : {fmt_ms(recovery_ms) if recovery_ms is not None else 'NEVER (within window)'}")
    print("-" * 74)
    print("VERDICT")
    # Two independent conditions must both hold for a peer to follow the VM:
    #   1. target provisioning: subif/MAC-VRF up and VM MAC learnt local on target
    #   2. fabric mobility: the source leaf stops claiming the VM as local
    if mac_local_t is None:
        print(f"  TARGET NOT PROVISIONED. After {rarps} RARP(s) (mode={args.rarp_mode}) the VM MAC never")
        print(f"  became local on {dst_leaf} within {args.timeout:.0f}s. The RARP(s) hit the port before the")
        print(f"  sub-interface/MAC-VRF were provisioned (subif up at {fmt(subif_up_t)}s) and were consumed")
        print(f"  as the active-VLAN trigger, not forwarded. A single/burst RARP is NOT sufficient — use")
        print(f"  sustained announcement (or continued VM traffic) spanning the provisioning window.")
    elif mobility_t is None:
        print(f"  TARGET UP BUT MOVE DID NOT PROPAGATE within {args.timeout:.0f}s. VM MAC is local on")
        print(f"  {dst_leaf} at t0+{mac_local_t:.2f}s, but {src_leaf} still claims it local (aging, not")
        print(f"  yet expired), so peers via {src_leaf} keep black-holing. The moved VM must keep sourcing")
        print(f"  traffic to win EVPN mobility — if it goes quiet, {src_leaf}'s stale local entry wins and")
        print(f"  the target yields to remote. Ensure sustained announcement is running (--rarp-mode")
        print(f"  sustained, the default) and raise --timeout; a real VM keeps sending and converges in")
        print(f"  a few seconds. (Also verify route-targets match across leaves — auto RT = local-ASN:EVI")
        print(f"  differs per leaf under an eBGP overlay; the handler pins it. --flush-source-leaf forces")
        print(f"  it but is NOT realistic — an ESXi trunk stays up when one VM leaves.)")
    else:
        print(f"  CONVERGED. Target learnt the VM at t0+{mac_local_t:.2f}s and the source leaf released it")
        print(f"  (EVPN mobility) at t0+{mobility_t:.2f}s.")
        if rarps_before_up:
            print(f"  Note: {rarps_before_up} of {rarps} RARPs were emitted before the sub-interface existed")
            print(f"  (up at t0+{fmt(subif_up_t)}s) and were dropped — a real single-shot vMotion RARP would")
            print(f"  have been lost; sustained announcement is what carried the trigger here.")
    print("=" * 74)

    if args.json_report:
        with open(args.json_report, "w") as f:
            json.dump({
                "vlan": vlan, "vm_mac": mac, "vm_ip": ip,
                "src": args.src, "src_leaf": src_leaf, "src_port": src_port,
                "dst": args.dst, "dst_leaf": dst_leaf, "dst_port": dst_port,
                "rarp_mode": args.rarp_mode, "rarps_total": rarps,
                "rarps_before_subif_up": rarps_before_up,
                "subif_up_s": subif_up_t, "mac_local_s": mac_local_t,
                "mobility_s": mobility_t, "flush_source_leaf": args.flush_source_leaf,
                "no_nudge": args.no_nudge,
                "src_mac_after": src_after, "peer": args.peer,
                "peer_outage_ms": outage_ms, "peer_recovery_ms": recovery_ms,
            }, f, indent=2)
        print(f"Wrote {args.json_report}")

    # ---- cleanup -----------------------------------------------------------------------
    if not args.keep:
        delete_subif(args.dst, parent, vlan)
        if args.peer:
            delete_subif(args.peer, parent, vlan)
        # source subif was already removed at cutover
        print("Cleaned up VM/peer sub-interfaces (leaf dynamic config self-clears via retention timer).")


def peer_id_to_mac(peer_id, vlan):
    return f"00:00:10:{peer_id:02x}:{(vlan >> 8) & 0xff:02x}:{vlan & 0xff:02x}"


def fmt(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) else "—"


def fmt_ms(x):
    return f"{x:.0f} ms" if isinstance(x, (int, float)) else "—"


if __name__ == "__main__":
    main()
