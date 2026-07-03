#!/usr/bin/env python3
import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

CLIENTS = {}

# sh-client1..10 on leaf1, sh-client11..20 on leaf3
for i in range(1, 21):
    CLIENTS[f"sh-client{i}"] = {
        "container": f"clab-srl-evpn-topo-sh-client{i}",
        "parent": "eth1",
        "id": i,
    }

# mh-client1..10 on leaf1/2, mh-client11..20 on leaf3/4
for i in range(1, 21):
    CLIENTS[f"mh-client{i}"] = {
        "container": f"clab-srl-evpn-topo-mh-client{i}",
        "parent": "bond0",
        "id": 20 + i,
    }

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

def run_cmd(container, cmd):
    full_cmd = ["docker", "exec", "-i", container, "sh"]
    res = subprocess.run(full_cmd, input=cmd, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr

def setup_client(name, info, vlans):
    container = info["container"]
    parent = info["parent"]
    cid = info["id"]
    
    print(f"Setting up {len(vlans)} VLANs on {name} ({container})...")
    
    # Build a shell script that uses ip -batch for bulk operations
    # Phase 1: create VLAN interfaces (ip -batch)
    # Phase 2: set MACs and IPs (ip -batch)
    script = f"""#!/bin/sh
set -e

# Phase 1: Create VLAN link interfaces
LINK_BATCH=$(mktemp)
ADDR_BATCH=$(mktemp)
trap 'rm -f $LINK_BATCH $ADDR_BATCH' EXIT

"""
    # Generate batch file content inline
    link_cmds = []
    addr_cmds = []
    for v in vlans:
        v_high = v // 100
        v_low = v % 100
        ip = f"10.{v_high}.{v_low}.{cid}/24"
        vlan_if = f"{parent}.{v}"
        mac = f"00:00:10:{cid:02x}:{v>>8:02x}:{v&0xff:02x}"
        
        link_cmds.append(f"link add link {parent} name {vlan_if} type vlan id {v}")
        link_cmds.append(f"link set dev {vlan_if} address {mac}")
        link_cmds.append(f"link set dev {vlan_if} up")
        addr_cmds.append(f"address add {ip} dev {vlan_if}")
    
    # Write batch commands via heredoc
    script += "cat > $LINK_BATCH << 'LINKEOF'\n"
    script += "\n".join(link_cmds) + "\n"
    script += "LINKEOF\n\n"
    
    script += "cat > $ADDR_BATCH << 'ADDREOF'\n"
    script += "\n".join(addr_cmds) + "\n"
    script += "ADDREOF\n\n"
    
    # Execute: ignore errors for already-existing interfaces (-force)
    script += "ip -batch $LINK_BATCH 2>/dev/null || true\n"
    script += "ip -batch $ADDR_BATCH 2>/dev/null || true\n"
    
    # Disable IPv6 on VLAN subinterfaces to prevent unsolicited traffic
    # (DAD, Router Solicitations, MLD reports) from keeping leaf dynamic
    # subinterfaces alive and preventing retention-timer cleanup.
    script += "\n# Disable IPv6 on VLAN subinterfaces\n"
    for v in vlans:
        vlan_if = f"{parent}.{v}"
        script += f"echo 1 > /proc/sys/net/ipv6/conf/{vlan_if}/disable_ipv6 2>/dev/null || true\n"
    
    code, out, err = run_cmd(container, script)
    if code != 0:
        print(f"Error setting up {name}: {err.strip()}", file=sys.stderr)
        return False
    return True

def cleanup_client(name, info, vlans):
    container = info["container"]
    parent = info["parent"]
    
    print(f"Cleaning up {len(vlans)} VLANs on {name} ({container})...")
    
    # Use ip -batch for bulk deletion
    batch_cmds = []
    for v in vlans:
        vlan_if = f"{parent}.{v}"
        batch_cmds.append(f"link delete {vlan_if}")
    
    script = "BATCH=$(mktemp)\ntrap 'rm -f $BATCH' EXIT\n"
    script += "cat > $BATCH << 'BATCHEOF'\n"
    script += "\n".join(batch_cmds) + "\n"
    script += "BATCHEOF\n"
    script += "ip -batch $BATCH 2>/dev/null || true\n"
    
    code, out, err = run_cmd(container, script)
    if code != 0:
        print(f"Error cleaning up {name}: {err.strip()}", file=sys.stderr)
        return False
    return True

def main():
    parser = argparse.ArgumentParser(description="Configure client VLAN interfaces on containerlab topology.")
    parser.add_argument("action", choices=["setup", "cleanup"], help="Action to perform")
    parser.add_argument("--vlans", default="1000-2000", help="VLAN list/range (default: 1000-2000)")
    parser.add_argument("--clients", default=None, help="Filter clients by prefix, e.g. 'sh' for sh-client* only")
    args = parser.parse_args()
    
    vlans = parse_vlans(args.vlans)
    if not vlans:
        print("No valid VLANs specified.", file=sys.stderr)
        sys.exit(1)
    
    # Filter clients by prefix if specified
    clients = CLIENTS
    if args.clients:
        prefix = args.clients + "-"
        clients = {k: v for k, v in CLIENTS.items() if k.startswith(prefix)}
        if not clients:
            print(f"No clients matching prefix '{args.clients}'", file=sys.stderr)
            sys.exit(1)
        
    print(f"Action: {args.action.upper()} for VLAN range {vlans[0]}..{vlans[-1]} (Total: {len(vlans)}, Clients: {len(clients)})")
    
    func = setup_client if args.action == "setup" else cleanup_client
    
    with ThreadPoolExecutor(max_workers=len(clients)) as executor:
        futures = {executor.submit(func, name, info, vlans): name for name, info in clients.items()}
        success = True
        for f in futures:
            name = futures[f]
            try:
                res = f.result()
                if not res:
                    success = False
            except Exception as e:
                print(f"Exception configuring {name}: {e}", file=sys.stderr)
                success = False
                
    if success:
        print("VLAN configuration finished successfully!")
    else:
        print("Some errors occurred during configuration.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
