#!/usr/bin/env python3
import argparse
import json
import select
import signal
import socket
import sys
import time

stop_requested = False

def handle_sig(signum, frame):
    global stop_requested
    stop_requested = True

signal.signal(signal.SIGINT, handle_sig)
signal.signal(signal.SIGTERM, handle_sig)

def run_listener(port, out_file, ready_file=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    except Exception as e:
        print(f"Warning: could not set SO_RCVBUF: {e}", file=sys.stderr, flush=True)

    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.1)

    # Signal that the socket is bound so the orchestrator can wait for real
    # readiness instead of racing a fixed sleep (which drops early packets and
    # inflates the measured convergence outage).
    if ready_file:
        try:
            with open(ready_file, 'w') as f:
                f.write("ok")
        except Exception as e:
            print(f"Warning: could not write ready file: {e}", file=sys.stderr, flush=True)

    print(f"Listener started on port {port}. Writing to {out_file}.", flush=True)
    raw_records = []
    
    while not stop_requested:
        try:
            data, addr = sock.recvfrom(1024)
            recv_time = time.time()
            raw_records.append((data, addr, recv_time))
        except socket.timeout:
            continue
        except Exception:
            pass
                
    sock.close()
    print(f"Listener stopped. Processing {len(raw_records)} raw packets...", flush=True)
    
    records = []
    for data, addr, recv_time in raw_records:
        try:
            payload = data.decode('utf-8', errors='ignore')
            parts = payload.split(',')
            if len(parts) == 4:
                records.append({
                    "src": addr[0],
                    "vlan": int(parts[0]),
                    "seq": int(parts[1]),
                    "send_time": float(parts[2]),
                    "recv_time": recv_time
                })
        except Exception:
            pass
            
    print(f"Saving {len(records)} parsed packets to {out_file}...", flush=True)
    try:
        with open(out_file, 'w') as f:
            json.dump(records, f)
        print("Save complete.", flush=True)
    except Exception as e:
        print(f"Error saving file: {e}", file=sys.stderr, flush=True)

def run_sender(targets_str, targets_file, duration, interval):
    targets = []
    if targets_file:
        try:
            with open(targets_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ip, vlan = line.split(':')
                        targets.append((ip, int(vlan)))
        except Exception as e:
            print(f"Error reading targets file: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
    elif targets_str:
        for part in targets_str.split(','):
            part = part.strip()
            if not part:
                continue
            ip, vlan = part.split(':')
            targets.append((ip, int(vlan)))
        
    if not targets:
        print("No targets specified for sender.", file=sys.stderr, flush=True)
        sys.exit(1)
        
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024 * 1024)
    except Exception as e:
        print(f"Warning: could not set SO_SNDBUF: {e}", file=sys.stderr, flush=True)
    sock.setblocking(False)
    
    print(f"Sender started. Sending to {len(targets)} targets for {duration}s at interval {interval}s...", flush=True)
    
    start_time = time.time()
    seq_num = 0
    total_packets = int(duration / interval)
    
    while seq_num < total_packets and not stop_requested:
        current_time = time.time()
        for ip, vlan in targets:
            payload = f"{vlan},{seq_num},{current_time},{interval}"
            try:
                sock.sendto(payload.encode('utf-8'), (ip, 5000))
            except Exception:
                pass
                
        seq_num += 1
        next_send = start_time + seq_num * interval
        sleep_time = next_send - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)
            
    sock.close()
    print(f"Sender stopped. Sent {seq_num} packets per target.", flush=True)

def main():
    parser = argparse.ArgumentParser(description="Traffic agent for containerlab clients.")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    
    # Listen subparser
    listen_parser = subparsers.add_parser("listen")
    listen_parser.add_argument("--port", type=int, default=5000)
    listen_parser.add_argument("--out", default="/tmp/traffic_agent_recv.json")
    listen_parser.add_argument("--ready-file", default=None,
                               help="File to create once the socket is bound")
    
    # Send subparser
    send_parser = subparsers.add_parser("send")
    group = send_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--targets", help="Comma-separated ip:vlan targets")
    group.add_argument("--targets-file", help="File containing ip:vlan targets (one per line)")
    send_parser.add_argument("--duration", type=float, default=10.0)
    send_parser.add_argument("--interval", type=float, default=0.01) # 10ms default
    
    args = parser.parse_args()
    
    if args.mode == "listen":
        run_listener(args.port, args.out, args.ready_file)
    elif args.mode == "send":
        run_sender(args.targets, args.targets_file, args.duration, args.interval)

if __name__ == "__main__":
    main()
