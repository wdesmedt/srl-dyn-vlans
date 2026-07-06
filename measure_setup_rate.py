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

Multi-interface (race) mode (--interfaces): trigger the SAME cold range on several ports of
the one node at once. All clients are launched together (docker exec spawned, then fed and
run near-simultaneously) so the leaf's event handler and active-VLAN monitor provision every
port concurrently. This is the way to check for races/contention between those two: with 1
port as a baseline, then 2, 3, ... ports, watch whether the aggregate provisioning rate holds
and whether any port drops VLANs (a per-interface "MISSING" line and uneven last-times are the
visible symptom). The report shows a per-interface breakdown plus an aggregate rate over the
combined pool of subifs. EVPN-tail (--dst-node) is single-interface only.

Examples:
  # local provisioning rate only (single interface)
  ./measure_setup_rate.py --node leaf1 --client sh-client1 --vlans 1000-1049
  # + inter-switch EVPN tail (leaf1 -> leaf3)
  ./measure_setup_rate.py --node leaf1 --client sh-client1 --vlans 1000-1049 \
      --dst-node leaf3 --dst-client sh-client11
  # race test: same range on 3 leaf1 ports at once (cid auto-derived from client name)
  ./measure_setup_rate.py --node leaf1 --vlans 1000-1049 \
      --interfaces sh-client1:ethernet-1/1,sh-client2:ethernet-1/2,sh-client3:ethernet-1/3
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

def derive_cid(client):
    """Client id from the trailing digits of a container name (sh-client7 -> 7)."""
    import re
    m = re.search(r"(\d+)$", client)
    return int(m.group(1)) if m else None

def parse_interfaces(spec, default_port):
    """Parse a comma-separated list of client[:port[:cid]] entries into
    [{'client','port','cid'}, ...]. port defaults to default_port; cid defaults to
    the client name's trailing digits. Raises ValueError on a malformed entry."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        fields = [f.strip() for f in part.split(":")]
        client = fields[0]
        if not client:
            raise ValueError(f"empty client in --interfaces entry '{part}'")
        port = fields[1] if len(fields) > 1 and fields[1] else default_port
        if len(fields) > 2 and fields[2]:
            cid = int(fields[2])
        else:
            cid = derive_cid(client)
            if cid is None:
                raise ValueError(f"cannot derive client-id from '{client}'; "
                                 f"use client:port:cid in --interfaces")
        out.append({"client": client, "port": port, "cid": cid})
    return out

def main():
    ap = argparse.ArgumentParser(description="Measure switch-side dynamic subinterface setup rate.")
    ap.add_argument("--node", required=True, help="Leaf container name (docker)")
    ap.add_argument("--leaf-mgmt", default=None,
                    help="Leaf gNMI mgmt IP (default: derived from --node via docker inspect)")
    ap.add_argument("--port", default="ethernet-1/1",
                    help="Trigger port for the single-interface case (default ethernet-1/1)")
    ap.add_argument("--client", default=None,
                    help="Client container that sources the trigger (single-interface case; "
                         "required unless --interfaces is given)")
    ap.add_argument("--vlans", required=True, help="Cold VLAN range, e.g. 1000-1049")
    ap.add_argument("--client-id", type=int, default=1, help="Client id for source IP (default 1)")
    # Multi-interface (race-condition) mode: trigger the SAME cold range on several ports of
    # the same node at once, so the event handler and the active-VLAN monitor provision all
    # of them concurrently. Reports per-interface and aggregate rates so contention or dropped
    # VLANs (a race symptom) show up.
    ap.add_argument("--interfaces", default=None,
                    help="Trigger multiple interfaces on --node simultaneously. Comma-separated "
                         "client[:port[:cid]] entries, e.g. "
                         "'sh-client1:ethernet-1/1,sh-client2:ethernet-1/2,sh-client3:ethernet-1/3'. "
                         "port defaults to --port (so entries must give distinct ports); cid "
                         "defaults to the client name's trailing digits. Overrides "
                         "--client/--port/--client-id. Not compatible with --dst-node.")
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

    # Resolve the set of trigger interfaces on --node. --interfaces (multi) overrides the
    # single-interface --client/--port/--client-id. All interfaces are on the same node and
    # are triggered with the SAME range simultaneously to stress the event handler / active-
    # VLAN monitor.
    if args.interfaces:
        try:
            interfaces = parse_interfaces(args.interfaces, args.port)
        except ValueError as e:
            print(f"ERROR: bad --interfaces: {e}", file=sys.stderr)
            sys.exit(1)
        if not interfaces:
            print("ERROR: --interfaces parsed to no entries.", file=sys.stderr)
            sys.exit(1)
        ports = [i["port"] for i in interfaces]
        if len(set(ports)) != len(ports):
            print("ERROR: duplicate port in --interfaces (each entry needs a distinct "
                  "port, e.g. client:port[:cid]).", file=sys.stderr)
            sys.exit(1)
        clients = [i["client"] for i in interfaces]
        if len(set(clients)) != len(clients):
            print("ERROR: duplicate client in --interfaces.", file=sys.stderr)
            sys.exit(1)
        cids = [i["cid"] for i in interfaces]
        if len(set(cids)) != len(cids):
            print(f"ERROR: duplicate client-id in --interfaces ({cids}); distinct ids are "
                  f"needed so source MACs/IPs don't collide — pass client:port:cid.",
                  file=sys.stderr)
            sys.exit(1)
    else:
        if not args.client:
            print("ERROR: --client is required (or use --interfaces).", file=sys.stderr)
            sys.exit(1)
        interfaces = [{"client": args.client, "port": args.port, "cid": args.client_id}]

    # EVPN-tail mode: resolve the destination leaf/client.
    evpn = bool(args.dst_node)
    if evpn and len(interfaces) > 1:
        print("ERROR: --dst-node (EVPN-tail) is not supported with multiple --interfaces; "
              "run the multi-interface race test without --dst-node.", file=sys.stderr)
        sys.exit(1)
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

    # 1b. Cold check for the specific target range on every trigger port
    cold_targets = [(args.node, mgmt, i["port"]) for i in interfaces]
    if evpn:
        cold_targets.append((args.dst_node, dst_mgmt, args.dst_port))
    for node, m, port in cold_targets:
        st = read_subif_state(m, user, pw, port)
        present = [v for v in vlans if v in st]
        if present:
            print(f"ERROR: {len(present)} of {N} target VLANs already exist on {node} "
                  f"{port} (range not cold). Use a fresh range or wait for retention.",
                  file=sys.stderr)
            sys.exit(1)
    src_ports = ",".join(i["port"] for i in interfaces)
    port_desc = f"{args.node} {src_ports}" + (f" + {args.dst_node} {args.dst_port}" if evpn else "")
    print(f"Range {vlans[0]}-{vlans[-1]} is cold on {port_desc} "
          f"({N} VLANs x {len(interfaces)} interface(s)).")

    # 2. Build the client sub-interfaces for the range. Bringing up a tagged
    #    sub-interface is itself the active-VLAN trigger, so creating them IS the
    #    stimulus (no separate traffic needed). The parent (eth1) must already exist;
    #    the range must NOT be pre-configured on the client (that would pre-trigger).
    #    In EVPN-tail mode the dst client is warmed too so the dst leaf provisions
    #    VLAN-<id> and can import the source MAC's Type-2 route.
    scripts = {i["client"]: build_trigger_script(args.parent, i["cid"], vlans)
               for i in interfaces}
    dst_script = build_trigger_script(args.parent, dst_cid, vlans) if evpn else None
    # Source MACs (EVPN-tail only, single interface): map each VLAN to the trigger MAC.
    src_macs = {v: client_mac(interfaces[0]["cid"], v).upper() for v in vlans}

    # 3. Trigger + timestamp. To hit the leaf on all interfaces at once (the whole point of
    #    the race test), spawn every client's `docker exec` first, then feed all their stdin
    #    and let them run near-simultaneously, rather than driving them one blocking call at
    #    a time.
    trigger_t = time.time()
    procs = []
    for i in interfaces:
        p = subprocess.Popen(["docker", "exec", "-i", i["client"], "sh"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, text=True)
        procs.append((p, i))
    for p, i in procs:
        p.stdin.write(scripts[i["client"]]); p.stdin.close()
    for p, _i in procs:
        p.wait()
    if evpn:
        subprocess.run(["docker", "exec", "-i", args.dst_client, "sh"],
                       input=dst_script, text=True, capture_output=True)
    print(f"Triggered active-VLAN detection at t0 (created {N} client subifs on "
          f"{len(interfaces)} interface(s){' + dst' if evpn else ''}); polling gNMI...")

    # 4. Poll until all N requested subinterfaces are oper-up on the src leaf (and, in
    #    EVPN-tail mode, until every source MAC is type=evpn in the dst FDB). The tool is
    #    agnostic to device config: it does not try to predict which VLANs will provision.
    #    Instead a settle timer stops the wait once provisioning stalls (no new subif/FDB
    #    entry for --settle s), so VLANs that never come up — excluded, out-of-range, or a
    #    detection miss — end the run promptly instead of blocking until --max-wait.
    deadline = trigger_t + args.max_wait
    fdb_appear = {}   # vlan -> host time the src MAC first appeared in dst VLAN-<id> FDB
    target = N * len(interfaces)  # total subifs expected across all trigger ports
    port_up = {i["port"]: set() for i in interfaces}   # port -> set(vlan) currently oper-up
    last_up, last_fdb = 0, 0
    last_progress_t = trigger_t
    stalled = False
    while time.time() < deadline:
        for i in interfaces:
            st = read_subif_state(mgmt, user, pw, i["port"])
            port_up[i["port"]] = {v for v in vlans if st.get(v, {}).get("oper") == "up"}
        n_up_now = sum(len(s) for s in port_up.values())
        if evpn:
            fdb = read_fdb(dst_mgmt, user, pw)
            now = time.time()
            for v in vlans:
                if v not in fdb_appear and fdb.get(f"VLAN-{v}", {}).get(src_macs[v]) == "evpn":
                    fdb_appear[v] = now
        if n_up_now != last_up or len(fdb_appear) != last_fdb:
            tail = f", dst-FDB {len(fdb_appear)}/{N}" if evpn else ""
            per = ("  [" + " ".join(f"{i['port'].split('/')[-1]}:{len(port_up[i['port']])}"
                                    for i in interfaces) + "]") if len(interfaces) > 1 else ""
            print(f"  t+{time.time()-trigger_t:5.1f}s: {n_up_now}/{target} up{tail}{per}")
            last_up, last_fdb = n_up_now, len(fdb_appear)
            last_progress_t = time.time()
        if n_up_now == target and (not evpn or len(fdb_appear) == N):
            break
        if time.time() - last_progress_t >= args.settle:
            stalled = True
            print(f"  no new provisioning for {args.settle:.0f}s — stopping wait "
                  f"({target-n_up_now} subif(s) not up; excluded/out-of-range/detection miss?).")
            break
        time.sleep(args.poll)

    # 5. Final read (per port) + compute per-subif setup time relative to trigger
    final_st = {i["port"]: read_subif_state(mgmt, user, pw, i["port"]) for i in interfaces}

    # Clean up the client subifs (the leaf keeps its dynamic config until the
    # retention timer expires; use a fresh range for the next measurement).
    if not args.keep_subifs:
        for client in [i["client"] for i in interfaces] + ([args.dst_client] if evpn else []):
            subprocess.run(["docker", "exec", client, "sh", "-c",
                            " ".join(f"ip link del {args.parent}.{v} 2>/dev/null;" for v in vlans)],
                           capture_output=True)

    def pctl(xs, p):
        xs = sorted(xs); k = (len(xs)-1)*p/100.0; lo = int(k); hi = min(lo+1, len(xs)-1)
        return xs[lo] + (xs[hi]-xs[lo])*(k-lo)

    # Per-interface setup times (relative to the shared trigger) + a combined pool for the
    # aggregate rate — the aggregate is what shows whether N ports at once still provision at
    # the same throughput or contend/starve each other.
    per_port = []
    all_ts = []
    for i in interfaces:
        st = final_st[i["port"]]
        s = [(v, st[v]["ts"] - trigger_t) for v in vlans if st.get(v, {}).get("ts")]
        s.sort(key=lambda x: x[1])
        up_v = set(v for v, _ in s)
        per_port.append({"port": i["port"], "client": i["client"], "cid": i["cid"],
                         "setup": s, "missing": sorted(vlanset - up_v),
                         "first": s[0][1] if s else None,
                         "last": s[-1][1] if s else None})
        all_ts += [t for _v, t in s]

    n_up = len(all_ts)
    if n_up == 0:
        print("No subinterfaces came up; nothing to measure.", file=sys.stderr)
        sys.exit(1)
    all_ts.sort()
    first, last = all_ts[0], all_ts[-1]
    spread = last - first if last > first else 0.0
    # provisioning rate over the ramp (exclude the fixed first-up latency)
    ramp_rate = (n_up - 1) / spread if spread > 0 else float("inf")
    overall_rate = n_up / last if last > 0 else float("inf")
    total_missing = sum(len(p["missing"]) for p in per_port)
    multi = len(interfaces) > 1
    # `setup` / `missing` kept as the single-interface view for the EVPN-tail section.
    setup = per_port[0]["setup"]
    missing = per_port[0]["missing"]

    print("\n" + "=" * 72)
    scope = f"{args.node}  ({len(interfaces)} interfaces x {N} VLANs)" if multi \
        else f"{args.node} {args.port}"
    print(f"SWITCH-SIDE SETUP RATE — {scope}")
    print("=" * 72)
    if multi:
        # Per-interface breakdown first: uneven last-times or per-port missing VLANs are the
        # visible symptom of a race between the event handler and the active-VLAN monitor.
        print("Per-interface:")
        for p in per_port:
            miss = (f"   MISSING {len(p['missing'])}: {p['missing'][:6]}"
                    f"{' ...' if len(p['missing']) > 6 else ''}") if p["missing"] else ""
            fu = f"{p['first']:.2f}" if p["first"] is not None else "  -  "
            lu = f"{p['last']:.2f}" if p["last"] is not None else "  -  "
            print(f"  {p['port']:<14} {p['client']:<12} : "
                  f"{len(p['setup'])}/{N} up   first {fu}s  last {lu}s{miss}")
        print("-" * 72)
        print("Aggregate (all interfaces):")
    print(f"Subifs requested       : {target}")
    print(f"Sub-interfaces up      : {n_up}")
    if total_missing:
        print(f"Not provisioned        : {total_missing}"
              f"   (excluded / out-of-range / detection miss — excluded from the rate)")
    print(f"First subif up  (t0+)  : {first:.2f} s   (fixed trigger+first-batch latency)")
    print(f"Last  subif up  (t0+)  : {last:.2f} s   (total setup time)")
    print(f"Setup-time p50 / p90   : {pctl(all_ts,50):.2f} / {pctl(all_ts,90):.2f} s")
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
        report = {"node": args.node, "num_vlans": N,
                  "num_interfaces": len(interfaces), "subifs_requested": target,
                  "n_up": n_up, "not_provisioned": total_missing, "stalled": stalled,
                  "first_s": first, "last_s": last,
                  "ramp_rate": ramp_rate, "overall_rate": overall_rate,
                  "interfaces": [
                      {"port": p["port"], "client": p["client"], "cid": p["cid"],
                       "n_up": len(p["setup"]), "missing": p["missing"],
                       "first_s": p["first"], "last_s": p["last"],
                       "setup_s": {v: t for v, t in p["setup"]}}
                      for p in per_port]}
        # Back-compat single-interface fields (first trigger port).
        report["port"] = per_port[0]["port"]
        report["setup_s"] = {v: t for v, t in per_port[0]["setup"]}
        if evpn:
            report.update({"dst_node": args.dst_node, "dst_port": args.dst_port,
                           "fdb_reached": len(fdb_appear),
                           "evpn_e2e_s": evpn_e2e, "evpn_tail_s": evpn_tail})
        with open(args.json_report, "w") as f:
            json.dump(report, f)
        print(f"Wrote {args.json_report}")

if __name__ == "__main__":
    main()
