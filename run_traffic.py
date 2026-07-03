#!/usr/bin/env python3
import argparse
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# File a listener creates once its socket is bound (see traffic_agent.py).
READY_FILE = "/tmp/traffic_agent_ready"

# These MUST match the leaf event handler (dynamic-subinterfaces.py): the
# handler applies at most VLAN_BATCH_SIZE subinterface operations per
# invocation and reinvokes after REINVOKE_DELAY_MS. They are used only to
# estimate a lower bound on dynamic setup time for the duration guard.
VLAN_BATCH_SIZE = 10
REINVOKE_DELAY_MS = 100

CLIENTS = {}

# sh-client1..10 on leaf1 (ports 1..10), sh-client11..20 on leaf3 (ports 1..10)
for i in range(1, 21):
    CLIENTS[f"sh-client{i}"] = {
        "container": f"clab-srl-evpn-topo-sh-client{i}",
        "parent": "eth1",
        "id": i,
    }

# mh-client1..10 on leaf1/2 (ports 11..20), mh-client11..20 on leaf3/4 (ports 11..20)
for i in range(1, 21):
    CLIENTS[f"mh-client{i}"] = {
        "container": f"clab-srl-evpn-topo-mh-client{i}",
        "parent": "bond0",
        "id": 20 + i,
    }

CLIENT_BY_ID = {info["id"]: name for name, info in CLIENTS.items()}

def parse_vlans(vlan_str):
    vlans = []
    for part in vlan_str.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            start, end = part.split('-')
            vlans.extend(range(int(start), int(end) + 1))
        elif '..' in part:
            start, end = part.split('..')
            vlans.extend(range(int(start), int(end) + 1))
        else:
            vlans.append(int(part))
    return sorted(list(set(vlans)))

def run_local_cmd(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr

def run_container_cmd(container, cmd_list, timeout=None):
    full_cmd = ["docker", "exec", container] + cmd_list
    try:
        res = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout expired"

def run_container_cmd_bg(container, cmd_list):
    full_cmd = ["docker", "exec", "-d", container] + cmd_list
    subprocess.run(full_cmd)

def get_client_ip(vlan, client_id):
    v_high = vlan // 100
    v_low = vlan % 100
    return f"10.{v_high}.{v_low}.{client_id}"

def generate_hotkeys(num_items):
    reserved = {'s', 'd', 'q', 'S', 'D', 'Q'}
    hotkeys = []
    for i in range(26):
        c = chr(ord('a') + i)
        if c not in reserved:
            hotkeys.append(c)
    for i in range(26):
        c = chr(ord('A') + i)
        if c not in reserved:
            hotkeys.append(c)
    for i in range(10):
        hotkeys.append(str(i))
    return hotkeys[:num_items]


def _max_leaf_port_load(active_clients):
    """Return the largest number of client-facing ports on any single leaf.

    Mirrors the topology: sh-client1..10 -> leaf1, sh-client11..20 -> leaf3;
    mh-client1..10 -> leaf1+leaf2, mh-client11..20 -> leaf3+leaf4. Each active
    port forces the leaf to instantiate one subinterface per tested VLAN, so
    the busiest leaf bounds the dynamic setup time.
    """
    counts = {}
    for name in active_clients:
        if name.startswith("sh-client"):
            idx = CLIENTS[name]["id"]
            leaves = ["leaf1"] if idx <= 10 else ["leaf3"]
        elif name.startswith("mh-client"):
            idx = CLIENTS[name]["id"] - 20
            leaves = ["leaf1", "leaf2"] if idx <= 10 else ["leaf3", "leaf4"]
        else:
            continue
        for leaf in leaves:
            counts[leaf] = counts.get(leaf, 0) + 1
    return max(counts.values()) if counts else 0


def compress_ranges(numbers):
    if not numbers:
        return "None"
    sorted_numbers = sorted(list(set(numbers)))
    ranges = []
    start = sorted_numbers[0]
    prev = start
    for n in sorted_numbers[1:]:
        if n == prev + 1:
            prev = n
        else:
            if start == prev:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{prev}")
            start = n
            prev = n
    if start == prev:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{prev}")
    return ", ".join(ranges)


def interactive_flow_selection(pairs):
    import sys
    # Check if stdin is a tty
    if not sys.stdin.isatty():
        print("Non-interactive session detected. Activating all flows.")
        return [True] * len(pairs)

    import shutil
    import termios
    import tty

    selected = [True] * len(pairs)
    fd = sys.stdin.fileno()
    hotkeys = generate_hotkeys(len(pairs))
    hotkey_index = {k: i for i, k in enumerate(hotkeys)}

    # Rendering is done inside the terminal's alternate screen buffer. Every
    # frame is drawn from the home position with an explicit clear, so the menu
    # never depends on cursor-up arithmetic (which corrupts the display when the
    # list is taller than the terminal window). When the flow list does not fit,
    # a scrolling viewport keeps the most recently toggled row visible.
    HEADER_LINES = 3
    FOOTER_LINES = 5  # separator + 3 control lines + trailing separator
    scroll_off = 0

    def draw():
        nonlocal scroll_off
        _cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        # Rows available for the flow list itself
        avail = max(1, rows - HEADER_LINES - FOOTER_LINES)
        n = len(pairs)

        # Clamp the scroll window so the list always fills the viewport when possible
        max_off = max(0, n - avail)
        if scroll_off > max_off:
            scroll_off = max_off
        end = min(n, scroll_off + avail)

        out = ["\033[H"]  # cursor home; each line is cleared to EOL as it is drawn
        def emit(text):
            out.append(text + "\033[K\r\n")

        emit("============================================================")
        if n > avail:
            emit(f"Interactive Flow Selection  (showing {scroll_off + 1}-{end} of {n}, [</>] scroll)")
        else:
            emit("Interactive Traffic Flow Selection (Toggle with keys)")
        emit("============================================================")
        for i in range(scroll_off, end):
            src, dst, is_uni = pairs[i]
            letter = hotkeys[i] if i < len(hotkeys) else "-"
            status = "[X]" if selected[i] else "[ ]"
            sep = "->" if is_uni else "<->"
            color = "32" if selected[i] else "90"
            emit(f"  {status} {letter}: \033[{color}m{src} {sep} {dst}\033[0m")
        emit("------------------------------------------------------------")
        emit("Controls: Press letter/number to toggle.")
        emit("          [s] Select All, [d] Deselect All, [</>] Scroll")
        emit("          [Enter] Run test, [q] Exit/Cancel")
        emit("============================================================")
        out.append("\033[J")  # clear anything left below (e.g. after a resize)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def reveal(idx):
        # Scroll so that flow index idx is within the current viewport
        nonlocal scroll_off
        _cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        avail = max(1, rows - HEADER_LINES - FOOTER_LINES)
        if idx < scroll_off:
            scroll_off = idx
        elif idx >= scroll_off + avail:
            scroll_off = idx - avail + 1

    old_settings = termios.tcgetattr(fd)
    # Enter alternate screen and hide cursor
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            draw()
            ch = sys.stdin.read(1)
            if ch == '\r' or ch == '\n':
                # Only allow proceeding if at least one flow is selected
                if any(selected):
                    break
            elif ch == 'q' or ch == '\x03':  # q or Ctrl-C
                # Restore terminal, leave alternate screen and exit
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                sys.stdout.write("\033[?25h\033[?1049l")
                sys.stdout.flush()
                print("Operation cancelled.")
                sys.exit(0)
            elif ch == 's':
                selected = [True] * len(pairs)
            elif ch == 'd':
                selected = [False] * len(pairs)
            elif ch in ('<', ','):
                scroll_off = max(0, scroll_off - 1)
            elif ch in ('>', '.'):
                scroll_off += 1
            elif ch in hotkey_index:
                idx = hotkey_index[ch]
                selected[idx] = not selected[idx]
                reveal(idx)
    finally:
        # Restore terminal, show cursor and leave alternate screen
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()

    return selected


def main():
    parser = argparse.ArgumentParser(description="Orchestrate traffic testing and measure convergence.")
    parser.add_argument("--vlans", default="1000-1002", help="VLAN list/range to test (default: 1000-1002)")
    parser.add_argument("--duration", type=float, default=10.0, help="Traffic duration in seconds (default: 10.0)")
    parser.add_argument("--interval", type=float, default=0.01, help="Packet interval in seconds (default: 0.01 = 10ms)")
    parser.add_argument("--flow", choices=["pairwise", "full-mesh"], default="pairwise", 
                        help="Flow topology: 'pairwise' (cross-fabric pairs) or 'full-mesh' (all-to-all)")
    parser.add_argument("--unidirectional", action="store_true", help="Use unidirectional traffic instead of default bidirectional traffic")
    parser.add_argument("--intraswitch", action="store_true", help="Use intra-leaf pairs (e.g. sh-client1<->sh-client2) instead of cross-fabric pairs")
    parser.add_argument("--no-setup", action="store_true", help="Skip automatic client VLAN subinterface configuration")
    parser.add_argument("--cleanup", action="store_true", help="Automatically cleanup client VLAN subinterfaces after test")
    parser.add_argument("--setup-delay", type=float, default=0, help="Seconds to wait after client VLAN setup for switch convergence (default: 0)")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-flow output, show only VLAN summary")
    parser.add_argument("--verbose", action="store_true", help="Force per-flow output even when VLAN count is large")
    parser.add_argument("--clients", default=None, help="Filter clients by prefix, e.g. 'sh' for sh-client* only, 'mh' for mh-client* only")
    parser.add_argument("--force", action="store_true", help="Override safety guards (cold unidirectional test, short-duration check)")
    parser.add_argument("--json-report", default=None, help="Write per-flow results (vlan, lost_count, max_outage_ms) to this JSON file")
    args = parser.parse_args()

    vlans = parse_vlans(args.vlans)
    if not vlans:
        print("No valid VLANs specified.", file=sys.stderr)
        sys.exit(1)

    # Guard #1: unidirectional flows cannot converge from a cold fabric. The far
    # leaf only instantiates the receiver's client subinterface/MAC-VRF once the
    # receiver sources a tagged frame. With a silent receiver (IPv6 disabled, no
    # GARP) the egress path is never built, so A->B is black-holed and reports
    # ~100% loss instead of a convergence time. It is only meaningful against an
    # already-warmed fabric (--no-setup).
    if args.unidirectional:
        print("=" * 60, file=sys.stderr)
        print("WARNING: --unidirectional under dynamic subinterface setup.", file=sys.stderr)
        print("  A one-way flow A->B cannot converge from cold: the far leaf only", file=sys.stderr)
        print("  instantiates B's subinterface/MAC-VRF when B itself sends a tagged", file=sys.stderr)
        print("  frame. A silent receiver means A->B is black-holed (~100% loss),", file=sys.stderr)
        print("  which is a topology artifact, not a switch convergence time.", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        if not args.no_setup and not args.force:
            print("Refusing cold unidirectional run. Use --no-setup on a pre-warmed", file=sys.stderr)
            print("fabric, drop --unidirectional, or pass --force to override.", file=sys.stderr)
            sys.exit(1)

    # Get all potential selectable items based on topology and direction options
    if args.flow == "pairwise":
        pairs = []
        if args.intraswitch:
            # Intra-leaf pairs: adjacent clients on the same leaf
            # leaf1: sh-client1<->sh-client2, sh-client3<->sh-client4, ..., sh-client9<->sh-client10
            for n in range(1, 10, 2):
                pairs.append((f"sh-client{n}", f"sh-client{n+1}"))
            # leaf3: sh-client11<->sh-client12, ..., sh-client19<->sh-client20
            for n in range(11, 20, 2):
                pairs.append((f"sh-client{n}", f"sh-client{n+1}"))
            # leaf1/2: mh-client1<->mh-client2, ..., mh-client9<->mh-client10
            for n in range(1, 10, 2):
                pairs.append((f"mh-client{n}", f"mh-client{n+1}"))
            # leaf3/4: mh-client11<->mh-client12, ..., mh-client19<->mh-client20
            for n in range(11, 20, 2):
                pairs.append((f"mh-client{n}", f"mh-client{n+1}"))
        else:
            # Cross-fabric pairs: sh-client{N} (leaf1) <-> sh-client{N+10} (leaf3)
            for n in range(1, 11):
                pairs.append((f"sh-client{n}", f"sh-client{n+10}"))
            # mh-client{N} (leaf1/2) <-> mh-client{N+10} (leaf3/4)
            for n in range(1, 11):
                pairs.append((f"mh-client{n}", f"mh-client{n+10}"))
        if args.clients:
            prefix = args.clients + "-"
            pairs = [(s, d) for s, d in pairs if s.startswith(prefix) or d.startswith(prefix)]
        if args.unidirectional:
            all_selectable = []
            for src, dst in pairs:
                all_selectable.append((src, dst, True))
                all_selectable.append((dst, src, True))
        else:
            all_selectable = [(src, dst, False) for src, dst in pairs]
    else:  # full-mesh
        client_names = list(CLIENTS.keys())
        if args.unidirectional:
            all_selectable = []
            for src in client_names:
                for dst in client_names:
                    if src != dst:
                        all_selectable.append((src, dst, True))
        else:
            all_selectable = []
            for i in range(len(client_names)):
                for j in range(i + 1, len(client_names)):
                    all_selectable.append((client_names[i], client_names[j], False))

    # Run interactive selection
    selected_mask = interactive_flow_selection(all_selectable)
    active_selectable = [all_selectable[i] for i in range(len(all_selectable)) if selected_mask[i]]
    
    # Build the final active unidirectional flows
    active_flows_uni = []
    for src, dst, is_uni in active_selectable:
        if is_uni:
            active_flows_uni.append((src, dst))
        else:
            active_flows_uni.append((src, dst))
            active_flows_uni.append((dst, src))
            
    active_clients = set()
    for src, dst in active_flows_uni:
        active_clients.add(src)
        active_clients.add(dst)

    # Guard #2: warn if --duration is too short for the leaves to finish
    # instantiating every subinterface. The busiest leaf configures
    # (ports x VLANs) subinterfaces, batched at VLAN_BATCH_SIZE per
    # REINVOKE_DELAY_MS, so any VLAN that converges after the test ends is
    # wrongly reported as a full outage. Only relevant when we drive cold setup.
    if not args.no_setup:
        max_ports = _max_leaf_port_load(active_clients)
        est_ops = max_ports * len(vlans)
        est_batches = math.ceil(est_ops / VLAN_BATCH_SIZE) if est_ops else 0
        est_setup_s = est_batches * (REINVOKE_DELAY_MS / 1000.0)
        recommended = est_setup_s * 1.5 + 5.0
        if est_setup_s > 0 and args.duration < recommended:
            print("=" * 60, file=sys.stderr)
            print("WARNING: --duration may be too short for dynamic setup.", file=sys.stderr)
            print(f"  Busiest leaf must instantiate ~{est_ops} subinterfaces "
                  f"({max_ports} port(s) x {len(vlans)} VLANs).", file=sys.stderr)
            print(f"  Estimated leaf setup time (lower bound): ~{est_setup_s:.1f}s.", file=sys.stderr)
            print(f"  VLANs converging after {args.duration:.0f}s will be reported as full", file=sys.stderr)
            print("  outages even though the switch is still setting them up.", file=sys.stderr)
            print(f"  Recommended --duration >= {recommended:.0f}.", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            if not args.force:
                if sys.stdin.isatty():
                    resp = input("Continue anyway? [y/N] ").strip().lower()
                    if resp not in ("y", "yes"):
                        print("Aborted.")
                        sys.exit(0)
                else:
                    print("  (proceeding; pass --force to silence or raise --duration)", file=sys.stderr)

    if not args.no_setup:
        print(f"Setting up client VLAN subinterfaces for {len(active_clients)} clients...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        setup_cmd = ["python3", os.path.join(script_dir, "configure_vlans.py"), "setup", "--vlans", args.vlans]
        if args.clients:
            setup_cmd.extend(["--clients", args.clients])
        code, out, err = run_local_cmd(setup_cmd)
        if code != 0:
            print(f"Error: Client VLAN setup failed: {err.strip()}", file=sys.stderr)
            sys.exit(1)

    # Optional explicit wait for switch convergence before starting traffic
    if args.setup_delay > 0:
        print(f"Waiting {args.setup_delay:.1f}s for switch convergence...")
        time.sleep(args.setup_delay)
        
    print("=" * 60)
    print(f"EVPN Traffic & Convergence Measurement Tool")
    print(f"VLANs to test: {vlans[0]}..{vlans[-1]} (Total: {len(vlans)})")
    print(f"Flow topology: {args.flow.upper()}")
    print(f"Direction    : {'UNIDIRECTIONAL' if args.unidirectional else 'BIDIRECTIONAL'}")
    print(f"Duration     : {args.duration}s")
    print(f"Interval     : {args.interval * 1000:.1f}ms ({int(1/args.interval)} pps per flow)")
    print(f"Active Flows :")
    for src, dst, is_uni in active_selectable:
        sep = "->" if is_uni else "<->"
        print(f"  - {src} {sep} {dst}")
    print("=" * 60)
    
    # 1. Deploy traffic_agent.py to active containers
    print("Deploying traffic agents to containers...")
    agent_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traffic_agent.py")
    for name in active_clients:
        info = CLIENTS[name]
        cmd = ["docker", "cp", agent_path, f"{info['container']}:/tmp/traffic_agent.py"]
        code, out, err = run_local_cmd(cmd)
        if code != 0:
            print(f"Failed to copy agent to {name}: {err.strip()}", file=sys.stderr)
            sys.exit(1)
            
    # 2. Stop any stale agent processes and delete old log files (incl. ready marker)
    for name in active_clients:
        info = CLIENTS[name]
        run_container_cmd(info["container"], ["pkill", "-9", "-f", "traffic_agent.py"])
        run_container_cmd(info["container"], ["rm", "-f", "/tmp/traffic_agent_recv.json", READY_FILE])

    # 3. Start listeners on active containers
    print("Starting traffic listeners...")
    for name in active_clients:
        info = CLIENTS[name]
        run_container_cmd_bg(info["container"], [
            "python3", "/tmp/traffic_agent.py", "listen",
            "--port", "5000",
            "--out", "/tmp/traffic_agent_recv.json",
            "--ready-file", READY_FILE
        ])

    # Guard #3: wait until every listener has actually bound its socket (ready
    # file appears) rather than racing a fixed sleep. A listener that binds
    # after the first packets arrive would otherwise drop them and inflate the
    # measured convergence outage.
    print("Waiting for listeners to bind...")
    pending = list(active_clients)
    deadline = time.time() + 10.0
    while pending and time.time() < deadline:
        still = []
        for name in pending:
            code, _, _ = run_container_cmd(CLIENTS[name]["container"], ["sh", "-c", f"test -f {READY_FILE}"])
            if code != 0:
                still.append(name)
        pending = still
        if pending:
            time.sleep(0.1)
    if pending:
        print(f"Warning: {len(pending)} listener(s) not confirmed ready after 10s: "
              f"{', '.join(sorted(pending))}", file=sys.stderr)

    # 4. Build flow targets
    # flows is a dict: src_name -> list of "ip:vlan"
    flows = {name: [] for name in CLIENTS}
    
    for v in vlans:
        for src, dst in active_flows_uni:
            dst_ip = get_client_ip(v, CLIENTS[dst]["id"])
            flows[src].append(f"{dst_ip}:{v}")
                    
    # Filter out clients that have no targets to send to
    active_senders = [name for name, targets in flows.items() if targets]
    
    # 4.5 Write targets files inside the containers
    print("Writing target files to containers...")
    for name in active_senders:
        container = CLIENTS[name]["container"]
        targets_content = "\n".join(flows[name])
        subprocess.run(
            ["docker", "exec", "-i", container, "sh", "-c", "cat > /tmp/traffic_targets.txt"],
            input=targets_content, text=True, capture_output=True
        )
    

    # 5. Start senders in parallel
    print("Generating traffic simultaneously across all flows...")
    start_time = time.time()
    
    def run_sender_thread(name):
        container = CLIENTS[name]["container"]
        cmd = [
            "python3", "/tmp/traffic_agent.py", "send",
            "--targets-file", "/tmp/traffic_targets.txt",
            "--duration", str(args.duration),
            "--interval", str(args.interval)
        ]
        run_container_cmd(container, cmd, timeout=args.duration + 10.0)
        
    with ThreadPoolExecutor(max_workers=len(active_senders)) as executor:
        list(executor.map(run_sender_thread, active_senders))
        
    # 6. Wait a moment for trailing packets, then stop listeners
    time.sleep(0.5)
    print("Stopping traffic listeners...")
    for name in active_clients:
        info = CLIENTS[name]
        run_container_cmd(info["container"], ["pkill", "-SIGINT", "-f", "traffic_agent.py listen"])
        
    # Wait for listeners to gracefully save their results
    time.sleep(1.0)
    
    # 7. Collect received packet logs
    print("Collecting received traffic logs...")
    received_logs = {}
    for name in active_clients:
        info = CLIENTS[name]
        code, out, err = run_container_cmd(info["container"], ["cat", "/tmp/traffic_agent_recv.json"])
        if code == 0 and out.strip():
            try:
                received_logs[name] = json.loads(out)
            except json.JSONDecodeError:
                print(f"Warning: Failed to parse logs from {name}")
                received_logs[name] = []
        else:
            received_logs[name] = []
            
    # 8. Analyze loss and convergence
    # Determine whether to show per-flow detail
    show_per_flow = args.verbose or (not args.quiet and len(vlans) <= 10)

    if show_per_flow:
        print("\n" + "=" * 90)
        print("TRAFFIC LOSS & CONVERGENCE REPORT")
        print("=" * 90)
        print(f"{'Flow':<44} {'TX':<8} {'RX':<8} {'Loss':<15} {'Status':<15}")
        print("-" * 90)
    else:
        print(f"\nAnalyzing {len(vlans)} VLANs x {len(active_flows_uni)} flows...")
    
    total_packets_expected = int(args.duration / args.interval)
    
    # Pre-index received logs by (src_ip, vlan) for O(1) lookups
    received_index = {}  # dst_name -> {(src_ip, vlan) -> set(seq_nums)}
    for dst_name, records in received_logs.items():
        idx = {}
        for p in records:
            key = (p["src"], p["vlan"])
            if key not in idx:
                idx[key] = set()
            idx[key].add(p["seq"])
        received_index[dst_name] = idx

    # Compile flows
    active_flows = []
    for src, dst in active_flows_uni:
        for v in vlans:
            active_flows.append((src, dst, v))
    active_flows.sort(key=lambda x: (x[2], x[0], x[1]))
    
    has_losses = False
    flow_results = []
    
    for src, dst, vlan in active_flows:
        src_id = CLIENTS[src]["id"]
        src_ip = get_client_ip(vlan, src_id)
        
        # O(1) lookup of received sequence numbers
        received_seqs = received_index.get(dst, {}).get((src_ip, vlan), set())
        
        sent_count = total_packets_expected
        received_count = len(received_seqs)
        lost_count = sent_count - received_count
        loss_pct = (lost_count / sent_count) * 100.0 if sent_count > 0 else 0
        
        # Find outages (convergence events)
        max_outage_ms = 0
        if lost_count > 0:
            has_losses = True
            in_outage = False
            outage_start_seq = -1
            for seq in range(total_packets_expected):
                if seq not in received_seqs:
                    if not in_outage:
                        in_outage = True
                        outage_start_seq = seq
                else:
                    if in_outage:
                        in_outage = False
                        dur_ms = (seq - outage_start_seq) * args.interval * 1000
                        if dur_ms > max_outage_ms:
                            max_outage_ms = dur_ms
            if in_outage:
                dur_ms = (total_packets_expected - outage_start_seq) * args.interval * 1000
                if dur_ms > max_outage_ms:
                    max_outage_ms = dur_ms

        if lost_count == 0:
            status_str = "OK"
        else:
            status_str = f"Outage ({max_outage_ms:.1f}ms)"
            
        if show_per_flow:
            flow_name = f"{src} -> {dst} (VLAN {vlan})"
            loss_val = f"{lost_count} ({loss_pct:.2f}%)"
            print(f"{flow_name:<44} {sent_count:<8} {received_count:<8} {loss_val:<15} {status_str:<15}")

        flow_results.append({
            "src": src,
            "dst": dst,
            "vlan": vlan,
            "lost_count": lost_count,
            "max_outage_ms": max_outage_ms
        })
        
    if show_per_flow:
        print("=" * 90)
    
    # 8.5 Group by VLAN for final summary
    ok_vlans = []
    outage_vlans = {}  # vlan -> list of dicts with lost flows
    
    for v in vlans:
        vlan_results = [r for r in flow_results if r["vlan"] == v]
        vlan_failures = [r for r in vlan_results if r["lost_count"] > 0]
        if not vlan_failures:
            ok_vlans.append(v)
        else:
            outage_vlans[v] = vlan_failures
            
    print("\n" + "=" * 90)
    print("VLAN SUMMARY")
    print("=" * 90)
    print(f"OK VLANs        : {compress_ranges(ok_vlans)}")
    if outage_vlans:
        # Group VLANs by their outage signature (flow directions + outage duration)
        outage_groups = {}  # signature -> list of VLANs
        for v in sorted(outage_vlans.keys()):
            # Build a signature from the failure details
            parts = []
            for r in sorted(outage_vlans[v], key=lambda x: (x["src"], x["dst"])):
                parts.append(f"{r['src']} -> {r['dst']} ({r['max_outage_ms']:.1f}ms)")
            sig = ", ".join(parts)
            outage_groups.setdefault(sig, []).append(v)
        
        print(f"Outage VLANs    : ({len(outage_vlans)} VLANs)")
        for sig, vlan_list in sorted(outage_groups.items(), key=lambda x: (-len(x[1]), x[0])):
            print(f"  {sig}")
            print(f"    {len(vlan_list)} VLAN(s): {compress_ranges(vlan_list)}")
    print("=" * 90)

    if args.json_report:
        report = {
            "meta": {
                "vlans": vlans,
                "num_vlans": len(vlans),
                "interval": args.interval,
                "duration": args.duration,
                "flow": args.flow,
                "intraswitch": args.intraswitch,
                "unidirectional": args.unidirectional,
                "clients": args.clients,
                "total_packets_expected": total_packets_expected,
            },
            "flows": flow_results,
        }
        with open(args.json_report, "w") as f:
            json.dump(report, f)
        print(f"Wrote per-flow JSON report to {args.json_report}")

    if args.cleanup:
        print("Cleaning up client VLAN subinterfaces...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cleanup_cmd = ["python3", os.path.join(script_dir, "configure_vlans.py"), "cleanup", "--vlans", args.vlans]
        if args.clients:
            cleanup_cmd.extend(["--clients", args.clients])
        run_local_cmd(cleanup_cmd)

if __name__ == "__main__":
    main()
