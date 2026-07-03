#!/usr/bin/env python3
"""
In-container RARP announcer — emulates the gratuitous RARP that VMware ESXi
sources on behalf of a migrated VM's vNIC at vMotion switchover.

Sends an Ethernet RARP request (EtherType 0x8035, opcode 3) with the VM's MAC as
both the sender and target hardware address and 0.0.0.0 as the protocol
addresses — byte-for-byte what vSphere emits so upstream switches relearn the
MAC on the new port. The frame is sent out a VLAN sub-interface (e.g. eth1.1055)
so the kernel adds the 802.1q tag; on SR Linux that tagged frame is also the
active-VLAN-detection trigger for dynamic sub-interfaces.

Usage: rarp_agent.py <iface> <vm-mac> [count] [interval_s]
  count <= 0 sends forever (until killed). Prints the number of frames sent.
"""
import socket
import struct
import sys
import time


def build_rarp(mac_bytes):
    # Ethernet: dst=broadcast, src=VM mac, ethertype=RARP(0x8035)
    eth = b"\xff\xff\xff\xff\xff\xff" + mac_bytes + struct.pack("!H", 0x8035)
    # RARP request: htype=1(Ether) ptype=0x0800(IPv4) hlen=6 plen=4 op=3(RARP req)
    body = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 3)
    body += mac_bytes + b"\x00\x00\x00\x00"   # sender hw = VM mac, sender proto 0.0.0.0
    body += mac_bytes + b"\x00\x00\x00\x00"   # target hw = VM mac, target proto 0.0.0.0
    return eth + body


def main():
    if len(sys.argv) < 3:
        print("usage: rarp_agent.py <iface> <vm-mac> [count] [interval_s]", file=sys.stderr)
        sys.exit(2)
    iface = sys.argv[1]
    mac = sys.argv[2]
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    interval = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0

    mac_bytes = bytes(int(o, 16) for o in mac.split(":"))
    frame = build_rarp(mac_bytes)

    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    s.bind((iface, 0))

    sent = 0
    infinite = count <= 0
    while infinite or sent < count:
        s.send(frame)
        sent += 1
        if not infinite and sent >= count:
            break
        if interval > 0:
            time.sleep(interval)
    print(sent)


if __name__ == "__main__":
    main()
