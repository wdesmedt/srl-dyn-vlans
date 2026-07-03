#!/usr/bin/env python3
"""
Switch-side dynamic sub-interface setup-rate measurement.

Unlike run_traffic.py (which infers convergence from client dataplane loss, and is
therefore sensitive to client ARP tables and host forwarding capacity), this tool
measures the *switch's* provisioning rate directly: it triggers active-VLAN detection
on a leaf port, then reads each sub-interface's `last-change` timestamp via gNMI and
computes when each subinterface came up relative to the trigger.

Because SR Linux containers share the host clock, the leaf's `last-change` (UTC) is
directly comparable to the host trigger time. No client ARP / dataplane is involved,
so the result reflects pure switch-side provisioning, independent of client/host
resource limits.

Why the subinterface `last-change` also stands in for the MAC-VRF and VXLAN interface:
the event handler (dyn_subif_custom.py / dynamic-subinterfaces.py) emits the per-VLAN
subinterface, its MAC-VRF network-instance (VLAN-<id>) and the vxlan0 vxlan-interface
into a *single* list of config actions per invocation, so they are applied in one atomic
transaction (up to VLAN_BATCH_SIZE=10 VLANs per commit). This is not incidental: the
MAC-VRF config references vxlan0.<id> before the vxlan-interface is created later in the
same action list, so the set only passes validation if it commits together. The
subinterface therefore cannot exist without its MAC-VRF and VXLAN having been created in
the same commit — its `last-change` is a reliable proxy for all three.

Caveat: `last-change` is an oper-state transition (creation/provisioning), not the VXLAN
tunnel becoming forwarding-ready nor cross-leaf EVPN route propagation.

EVPN-tail mode (optional, --dst-node): additionally measure the inter-switch tail. The
same range is warmed on a destination leaf/client (so the dst leaf provisions VLAN-<id>
and can import the route), and the tool times when each source client's MAC lands in the
dst leaf's VLAN-<id> mac-table with type 'evpn' (BGP-EVPN Type-2 propagated + remote FDB
programmed). It reports end-to-end (t0 -> MAC in dst FDB) and the EVPN tail (that minus
the local subif-up time). This only converges with matched route-targets across the
overlay (the rt-asn fix in dyn_subif_custom.py). dst-FDB appearance is host-poll observed,
so its resolution is bounded by --poll.

Requirements: gnmic on the host; the leaf reachable on gNMI (default clab
admin/NokiaSrl1!, port 57400, skip-verify).

Trigger: the tool CREATES the client sub-interfaces for the range (bringing up a tagged
sub-interface is itself the active-VLAN trigger on SR Linux), so the client only needs
its parent interface (eth1) — the range must NOT be pre-configured on the client, and
must be cold on the leaf. Use a range not triggered within the retention-timer window
(default 240 min) or wait for retention. The tool refuses to run if the range is already
up on the leaf, and deletes the client subifs afterwards (unless --keep-subifs).

Not every requested VLAN necessarily provisions (some may be excluded by the handler,
outside the vlan-range, or missed by detection). The tool stays agnostic to device config
and does not try to predict which ones: a settle timer (--settle) stops the wait once
provisioning stalls (no new subif/FDB entry for that long), so such a range ends promptly
and reports what came up instead of hanging until --max-wait.

The *requested range* must be cold: no VLAN in it may already be detected active
(`interface … dynamic-subinterfaces active-vlans`) on any port, else its subifs already
exist and the timing is meaningless. The tool refuses to run if any requested VLAN is
already active. Active VLANs *outside* the range (e.g. leftovers from a previous run
still within their retention-timer) are allowed — they share the provisioning pipeline
so they may add minor contention, which the tool notes but does not block on. Use
--allow-active-vlans to skip the check entirely.

Examples:
  # local provisioning rate only
  ./measure_setup_rate.py --node leaf1 --client sh-client1 --vlans 1000-1049
  # + inter-switch EVPN tail (leaf1 -> leaf3)
  ./measure_setup_rate.py --node leaf1 --client sh-client1 --vlans 1000-1049 \
      --dst-node leaf3 --dst-client sh-client11
"""
import argparse, json, os, subprocess, sys, time

def sh(cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def docker_mgmt_ip(node):
    """Return the management IP of a container by name, via docker inspect.
    Picks the first non-empty address across the container's networks."""
    code, out, _ = sh(["docker", "inspect", "-f",
                       "{{range .NetworkSettings.Networks}}{{.IPAddress}}\n{{end}}", node])
    if code != 0:
        return None
    for line in out.splitlines():
        ip = line.strip()
        if ip:
            return ip
    return None

def gnmic_get(mgmt, user, pw, paths, timeout=60):
    cmd = ["gnmic", "-a", f"{mgmt}:57400", "-u", user, "-p", pw, "--skip-verify",
           "-e", "json_ietf", "get", "--type", "state"]
    for p in paths:
        cmd += ["--path", p]
    code, out, err = sh(cmd, timeout=timeout)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None

def read_active_vlans(mgmt, user, pw):
    """Return {port: [vlan, ...]} for every port with detected active VLANs.

    Reads `/interface/dynamic-subinterfaces/active-vlans` fabric-wide. Used to confirm
    the requested range is cold (none of its VLANs already active); active VLANs outside
    the range only share the provisioning pipeline (minor contention), they don't
    invalidate the measurement."""
    data = gnmic_get(mgmt, user, pw,
                     ["/interface[name=*]/dynamic-subinterfaces/active-vlans"])
    result = {}
    if not data:
        return result
    for src in data:
        for upd in src.get("updates", []):
            for _k, v in upd.get("values", {}).items():
                ifs = v.get("srl_nokia-interfaces:interface") or v.get("interface") or []
                if isinstance(ifs, dict):
                    ifs = [ifs]
                for it in ifs:
                    name = it.get("name")
                    ds = None
                    for kk, vv in it.items():
                        if kk.endswith("dynamic-subinterfaces"):
                            ds = vv
                    av = (ds or {}).get("active-vlans") or []
                    if av:
                        result[name] = av
    return result

def read_subif_state(mgmt, user, pw, port):
    """Return {vlan_index: {'oper':..., 'last_change_ns':...}} for a port."""
    data = gnmic_get(mgmt, user, pw,
                     [f"/interface[name={port}]/subinterface"])
    result = {}
    if not data:
        return result
    for src in data:
        for upd in src.get("updates", []):
            val = upd.get("values", {})
            for _k, v in val.items():
                subs = v.get("srl_nokia-interfaces:subinterface") or v.get("subinterface") or []
                if isinstance(subs, dict):
                    subs = [subs]
                for s in subs:
                    idx = s.get("index")
                    if idx is None:
                        continue
                    lc = s.get("last-change")
                    ns = None
                    if lc:
                        # RFC3339 -> epoch ns
                        import datetime
                        t = lc.replace("Z", "+00:00")
                        # trim to microseconds for fromisoformat
                        if "." in t:
                            head, frac = t.split(".")
                            fracdigits = frac.split("+")[0]
                            tz = frac[len(fracdigits):]
                            frac6 = (fracdigits + "000000")[:6]
                            t = f"{head}.{frac6}{tz}"
                        ns = datetime.datetime.fromisoformat(t).timestamp()
                    result[int(idx)] = {"oper": s.get("oper-state"), "ts": ns}
    return result

def read_fdb(mgmt, user, pw):
    """Return {ni_name: {MAC_UPPER: type}} from every network-instance's mac-table.

    Used for the EVPN-tail measurement: a source MAC appearing in the *destination*
    leaf's VLAN-<id> mac-table with type 'evpn' means the BGP-EVPN Type-2 route
    propagated and the remote FDB is programmed."""
    data = gnmic_get(mgmt, user, pw,
                     ["/network-instance[name=*]/bridge-table/mac-table"])
    result = {}
    if not data:
        return result
    for src in data:
        for upd in src.get("updates", []):
            for _k, v in upd.get("values", {}).items():
                nis = v.get("srl_nokia-network-instance:network-instance") or v.get("network-instance") or []
                if isinstance(nis, dict):
                    nis = [nis]
                for ni in nis:
                    bt = next((vv for k, vv in ni.items() if k.endswith("bridge-table")), None)
                    if not bt:
                        continue
                    mt = next((vv for k, vv in bt.items() if k.endswith("mac-table")), None)
                    if not mt:
                        continue
                    macs = mt.get("mac") or []
                    if isinstance(macs, dict):
                        macs = [macs]
                    result[ni.get("name")] = {m["address"].upper(): m.get("type")
                                              for m in macs if m.get("address")}
    return result

def client_mac(cid, vlan):
    """Deterministic client MAC for (client-id, vlan) — must match the trigger frame's
    source MAC so it can be located in a remote leaf's FDB."""
    return f"00:00:10:{cid:02x}:{vlan>>8:02x}:{vlan&0xff:02x}"

def build_trigger_script(parent, cid, vlans):
    """Shell script that creates the tagged client sub-interfaces for the range. Bringing
    up a tagged sub-interface is itself the active-VLAN trigger; the frame it emits carries
    client_mac(cid, vlan) as source, which is what the leaf learns and EVPN advertises."""
    links, addrs = [], []
    for v in vlans:
        vif = f"{parent}.{v}"
        links += [f"link add link {parent} name {vif} type vlan id {v}",
                  f"link set dev {vif} address {client_mac(cid, v)}", f"link set dev {vif} up"]
        addrs.append(f"address add 10.{v//100}.{v%100}.{cid}/24 dev {vif}")
    return ("cat > /tmp/_lb <<'E'\n" + "\n".join(links) + "\nE\n"
            "cat > /tmp/_ab <<'E'\n" + "\n".join(addrs) + "\nE\n"
            "ip -batch /tmp/_lb\nip -batch /tmp/_ab\n")

def parse_vlans(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-"); out += range(int(a), int(b)+1)
        elif part:
            out.append(int(part))
    return sorted(set(out))

def main():
    ap = argparse.ArgumentParser(description="Measure switch-side dynamic subinterface setup rate.")
    ap.add_argument("--node", required=True, help="Leaf container name (docker)")
    ap.add_argument("--leaf-mgmt", default=None,
                    help="Leaf gNMI mgmt IP (default: derived from --node via docker inspect)")
    ap.add_argument("--port", default="ethernet-1/1")
    ap.add_argument("--client", required=True, help="Client container that sources the trigger")
    ap.add_argument("--vlans", required=True, help="Cold VLAN range, e.g. 1000-1049")
    ap.add_argument("--client-id", type=int, default=1, help="Client id for source IP (default 1)")
    ap.add_argument("--parent", default="eth1", help="Client parent interface (default eth1)")
    ap.add_argument("--keep-subifs", action="store_true", help="Do not delete the client subifs after measuring")
    # EVPN-tail (inter-switch) measurement: also warm the same range on a destination
    # leaf/client, then time when each source MAC lands in the dst leaf's VLAN-<id> FDB
    # via BGP-EVPN. Enabled only when --dst-node is given.
    ap.add_argument("--dst-node", default=None,
                    help="Destination leaf container for EVPN-tail measurement (enables it)")
    ap.add_argument("--dst-mgmt", default=None,
                    help="Dst leaf gNMI mgmt IP (default: derived from --dst-node)")
    ap.add_argument("--dst-client", default=None,
                    help="Client on the dst leaf that provisions VLAN-<id> there (required with --dst-node)")
    ap.add_argument("--dst-port", default="ethernet-1/1")
    ap.add_argument("--dst-client-id", type=int, default=None,
                    help="Dst client id for its IP/MAC (default: --client-id + 10)")
    ap.add_argument("--allow-active-vlans", action="store_true",
                    help="Skip the range-coldness active-VLANs check (allow requested "
                         "VLANs to already be active; not recommended)")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="NokiaSrl1!")
    ap.add_argument("--poll", type=float, default=0.5, help="gNMI poll interval (s)")
    ap.add_argument("--max-wait", type=float, default=120.0, help="Hard cap: give up after this many s")
    ap.add_argument("--settle", type=float, default=10.0,
                    help="Stop waiting once no new subif/FDB entry appears for this many s "
                         "(guards against hanging on VLANs that never provision)")
    ap.add_argument("--json-report", default=None)
    args = ap.parse_args()

    vlans = parse_vlans(args.vlans)
    N = len(vlans)
    mgmt = args.leaf_mgmt or docker_mgmt_ip(args.node)
    if not mgmt:
        print(f"ERROR: could not determine gNMI mgmt IP for node '{args.node}' via "
              f"docker inspect; pass --leaf-mgmt explicitly.", file=sys.stderr)
        sys.exit(1)
    if not args.leaf_mgmt:
        print(f"Derived gNMI mgmt IP {mgmt} for {args.node}.")
    user, pw = args.user, args.password

    # EVPN-tail mode: resolve the destination leaf/client.
    evpn = bool(args.dst_node)
    dst_mgmt = None
    dst_cid = None
    if evpn:
        if not args.dst_client:
            print("ERROR: --dst-client is required with --dst-node.", file=sys.stderr)
            sys.exit(1)
        dst_mgmt = args.dst_mgmt or docker_mgmt_ip(args.dst_node)
        if not dst_mgmt:
            print(f"ERROR: could not determine gNMI mgmt IP for dst node "
                  f"'{args.dst_node}'; pass --dst-mgmt explicitly.", file=sys.stderr)
            sys.exit(1)
        dst_cid = args.dst_client_id if args.dst_client_id is not None else args.client_id + 10
        if dst_cid == args.client_id and args.dst_client == args.client:
            print("ERROR: dst client/id collide with the source; use a distinct "
                  "--dst-client and/or --dst-client-id.", file=sys.stderr)
            sys.exit(1)
        if not args.dst_mgmt:
            print(f"Derived gNMI mgmt IP {dst_mgmt} for {args.dst_node}.")

    # VLAN IDs must be valid 802.1q (1..4094) and within the leaf's configured
    # dynamic-subinterfaces vlan-range, or the subifs are never created.
    bad = [v for v in vlans if v < 1 or v > 4094]
    if bad:
        print(f"ERROR: invalid 802.1q VLAN IDs (must be 1..4094): {bad[:5]}...",
              file=sys.stderr)
        sys.exit(1)

    # 1a. Range coldness via active-VLANs: the requested range must be cold — no VLAN in
    #     it may already be detected active (on any port), or its subifs already exist and
    #     the timing is meaningless. Active VLANs *outside* the range are allowed (e.g.
    #     leftovers from a previous run still within retention); they share the
    #     provisioning pipeline so they may add minor contention, which we note but do not
    #     block on. --allow-active-vlans skips the check entirely.
    check_nodes = [(args.node, mgmt)]
    if evpn:
        check_nodes.append((args.dst_node, dst_mgmt))
    vlanset = set(vlans)
    if not args.allow_active_vlans:
        for node, m in check_nodes:
            active = read_active_vlans(m, user, pw)
            active_ids = set().union(*active.values()) if active else set()
            in_range = sorted(vlanset & active_ids)
            if in_range:
                where = ", ".join(f"{p}:{len(set(lst) & vlanset)}"
                                  for p, lst in sorted(active.items()) if set(lst) & vlanset)
                print(f"ERROR: {len(in_range)} of {N} requested VLANs are already active on "
                      f"{node} ({where}); e.g. {in_range[:10]} — the range is not cold. Use "
                      f"a fresh range, wait for retention, or pass --allow-active-vlans to "
                      f"override.", file=sys.stderr)
                sys.exit(1)
            out_of_range = len(active_ids - vlanset)
            if out_of_range:
                print(f"Note: {node} has {out_of_range} active VLAN(s) outside the requested "
                      f"range (leftover / other ports); proceeding — they share the "
                      f"provisioning pipeline and may add minor contention.")
        print(f"Requested range is cold (no in-range active VLANs) on "
              f"{'/'.join(n for n, _ in check_nodes)}.")

    # 1b. Cold check for the specific target range on the trigger port(s)
    for node, m, port in ([(args.node, mgmt, args.port)] +
                          ([(args.dst_node, dst_mgmt, args.dst_port)] if evpn else [])):
        st = read_subif_state(m, user, pw, port)
        present = [v for v in vlans if v in st]
        if present:
            print(f"ERROR: {len(present)} of {N} target VLANs already exist on {node} "
                  f"{port} (range not cold). Use a fresh range or wait for retention.",
                  file=sys.stderr)
            sys.exit(1)
    port_desc = (f"{args.node} {args.port}" if not evpn
                 else f"{args.node} {args.port} + {args.dst_node} {args.dst_port}")
    print(f"Range {vlans[0]}-{vlans[-1]} is cold on {port_desc} ({N} VLANs).")

    # 2. Build the client sub-interfaces for the range. Bringing up a tagged
    #    sub-interface is itself the active-VLAN trigger, so creating them IS the
    #    stimulus (no separate traffic needed). The parent (eth1) must already exist;
    #    the range must NOT be pre-configured on the client (that would pre-trigger).
    #    In EVPN-tail mode the dst client is warmed too so the dst leaf provisions
    #    VLAN-<id> and can import the source MAC's Type-2 route.
    src_script = build_trigger_script(args.parent, args.client_id, vlans)
    dst_script = build_trigger_script(args.parent, dst_cid, vlans) if evpn else None
    src_macs = {v: client_mac(args.client_id, v).upper() for v in vlans}

    # 3. Trigger + timestamp
    trigger_t = time.time()
    subprocess.run(["docker", "exec", "-i", args.client, "sh"],
                   input=src_script, text=True, capture_output=True)
    if evpn:
        subprocess.run(["docker", "exec", "-i", args.dst_client, "sh"],
                       input=dst_script, text=True, capture_output=True)
    print(f"Triggered active-VLAN detection at t0 (created {N} client subifs"
          f"{' on src+dst' if evpn else ''}); polling gNMI...")

    # 4. Poll until all N requested subinterfaces are oper-up on the src leaf (and, in
    #    EVPN-tail mode, until every source MAC is type=evpn in the dst FDB). The tool is
    #    agnostic to device config: it does not try to predict which VLANs will provision.
    #    Instead a settle timer stops the wait once provisioning stalls (no new subif/FDB
    #    entry for --settle s), so VLANs that never come up — excluded, out-of-range, or a
    #    detection miss — end the run promptly instead of blocking until --max-wait.
    deadline = trigger_t + args.max_wait
    fdb_appear = {}   # vlan -> host time the src MAC first appeared in dst VLAN-<id> FDB
    last_up, last_fdb = 0, 0
    last_progress_t = trigger_t
    stalled = False
    while time.time() < deadline:
        st = read_subif_state(mgmt, user, pw, args.port)
        up = [v for v in vlans if st.get(v, {}).get("oper") == "up"]
        if evpn:
            fdb = read_fdb(dst_mgmt, user, pw)
            now = time.time()
            for v in vlans:
                if v not in fdb_appear and fdb.get(f"VLAN-{v}", {}).get(src_macs[v]) == "evpn":
                    fdb_appear[v] = now
        if len(up) != last_up or len(fdb_appear) != last_fdb:
            tail = f", dst-FDB {len(fdb_appear)}/{N}" if evpn else ""
            print(f"  t+{time.time()-trigger_t:5.1f}s: {len(up)}/{N} up{tail}")
            last_up, last_fdb = len(up), len(fdb_appear)
            last_progress_t = time.time()
        if len(up) == N and (not evpn or len(fdb_appear) == N):
            break
        if time.time() - last_progress_t >= args.settle:
            stalled = True
            print(f"  no new provisioning for {args.settle:.0f}s — stopping wait "
                  f"({N-len(up)} VLAN(s) not up; excluded/out-of-range/detection miss?).")
            break
        time.sleep(args.poll)

    # 5. Final read + compute per-subif setup time relative to trigger
    st = read_subif_state(mgmt, user, pw, args.port)

    # Clean up the client subifs (the leaf keeps its dynamic config until the
    # retention timer expires; use a fresh range for the next measurement).
    if not args.keep_subifs:
        for client in [args.client] + ([args.dst_client] if evpn else []):
            subprocess.run(["docker", "exec", client, "sh", "-c",
                            " ".join(f"ip link del {args.parent}.{v} 2>/dev/null;" for v in vlans)],
                           capture_output=True)

    setup = []
    for v in vlans:
        e = st.get(v, {})
        if e.get("ts"):
            setup.append((v, e["ts"] - trigger_t))
    if not setup:
        print("No subinterfaces came up; nothing to measure.", file=sys.stderr)
        sys.exit(1)

    times = sorted(t for _v, t in setup)
    n_up = len(setup)
    first, last = times[0], times[-1]
    spread = last - first if last > first else 0.0
    # provisioning rate over the ramp (exclude the fixed first-up latency)
    ramp_rate = (n_up - 1) / spread if spread > 0 else float("inf")
    overall_rate = n_up / last if last > 0 else float("inf")

    def pctl(xs, p):
        xs = sorted(xs); k = (len(xs)-1)*p/100.0; lo = int(k); hi = min(lo+1, len(xs)-1)
        return xs[lo] + (xs[hi]-xs[lo])*(k-lo)

    up_vlans = set(v for v, _ in setup)
    missing = sorted(vlanset - up_vlans)

    print("\n" + "=" * 72)
    print(f"SWITCH-SIDE SETUP RATE — {args.node} {args.port}")
    print("=" * 72)
    print(f"VLANs requested        : {N}")
    print(f"Sub-interfaces up      : {n_up}")
    if missing:
        print(f"Not provisioned        : {len(missing)}   {missing[:10]}"
              f"{' ...' if len(missing) > 10 else ''}")
        print(f"                         (excluded / out-of-range / detection miss — "
              f"excluded from the rate)")
    print(f"First subif up  (t0+)  : {first:.2f} s   (fixed trigger+first-batch latency)")
    print(f"Last  subif up  (t0+)  : {last:.2f} s   (total setup time)")
    print(f"Setup-time p50 / p90   : {pctl([t for _v,t in setup],50):.2f} / "
          f"{pctl([t for _v,t in setup],90):.2f} s")
    print(f"Ramp provisioning rate : {ramp_rate:.1f} subif/s  (over the {spread:.1f}s ramp)")
    print(f"Overall rate (incl t0) : {overall_rate:.1f} subif/s")
    print("=" * 72)

    # 6. EVPN tail (inter-switch): end-to-end = t0 -> src MAC in dst FDB; tail = that
    #    minus the local src subif-up time. dst-FDB appearance is host-poll observed
    #    (granularity ~= --poll), the local time is the device last-change, both on the
    #    shared host clock.
    evpn_e2e, evpn_tail = {}, {}
    if evpn:
        local_up = {v: t for v, t in setup}
        for v, ts in fdb_appear.items():
            e2e = ts - trigger_t
            evpn_e2e[v] = e2e
            if v in local_up:
                evpn_tail[v] = e2e - local_up[v]
        print(f"EVPN TAIL (inter-switch) — {args.node} -> {args.dst_node}")
        print("=" * 72)
        print(f"MACs in dst FDB (evpn) : {len(fdb_appear)}/{N}")
        if evpn_e2e:
            e2e_vals = list(evpn_e2e.values()); tail_vals = list(evpn_tail.values())
            print(f"End-to-end p50 / p90   : {pctl(e2e_vals,50):.2f} / {pctl(e2e_vals,90):.2f} s"
                  f"   (t0 -> MAC in dst FDB)")
            print(f"Last MAC in dst FDB    : {max(e2e_vals):.2f} s   (total inter-switch convergence)")
            if tail_vals:
                print(f"EVPN tail p50 / p90    : {pctl(tail_vals,50):.2f} / {pctl(tail_vals,90):.2f} s"
                      f"   (dst FDB - local subif-up)")
        if len(fdb_appear) < N:
            print(f"NOTE: {N-len(fdb_appear)} of {N} MAC(s) never reached the dst FDB — "
                  f"VLAN not provisioned on both leaves (excluded/out-of-range), RT import "
                  f"(rt-asn), or raise --settle/--max-wait.")
        print("=" * 72)

    if args.json_report:
        report = {"node": args.node, "port": args.port, "num_vlans": N,
                  "n_up": n_up, "not_provisioned": missing, "stalled": stalled,
                  "first_s": first, "last_s": last,
                  "ramp_rate": ramp_rate, "overall_rate": overall_rate,
                  "setup_s": {v: t for v, t in setup}}
        if evpn:
            report.update({"dst_node": args.dst_node, "dst_port": args.dst_port,
                           "fdb_reached": len(fdb_appear),
                           "evpn_e2e_s": evpn_e2e, "evpn_tail_s": evpn_tail})
        with open(args.json_report, "w") as f:
            json.dump(report, f)
        print(f"Wrote {args.json_report}")

if __name__ == "__main__":
    main()
