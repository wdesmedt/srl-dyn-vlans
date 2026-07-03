import os

def gen_spine(spine_id):
    cfg = []
    cfg.append(f"set / interface system0 admin-state enable")
    cfg.append(f"set / interface system0 subinterface 0 admin-state enable")
    cfg.append(f"set / interface system0 subinterface 0 ipv4 admin-state enable")
    cfg.append(f"set / interface system0 subinterface 0 ipv4 address 100.0.0.{10+spine_id}/32")
    cfg.append(f"set / network-instance default protocols bgp admin-state enable")
    cfg.append(f"set / network-instance default protocols bgp router-id 100.0.0.{10+spine_id}")
    cfg.append(f"set / network-instance default protocols bgp autonomous-system 65000")
    cfg.append(f"set / network-instance default type default")
    cfg.append(f"set / network-instance default interface system0.0")
    
    cfg.append(f"set / network-instance default protocols bgp group leaves")
    cfg.append(f"set / network-instance default protocols bgp afi-safi ipv4-unicast admin-state enable")
    cfg.append(f"set / network-instance default protocols bgp afi-safi ipv6-unicast admin-state enable")
    cfg.append(f"set / network-instance default protocols bgp afi-safi evpn admin-state enable")
    # For EVPN over IPv6 next-hops
    cfg.append(f"set / network-instance default protocols bgp afi-safi ipv6-unicast ipv4-unicast advertise-ipv6-next-hops true")
    cfg.append(f"set / network-instance default protocols bgp afi-safi evpn evpn advertise-ipv6-next-hops true")
    cfg.append(f"set / network-instance default protocols bgp afi-safi evpn evpn keep-all-routes true")
    cfg.append(f"set / network-instance default protocols bgp afi-safi evpn evpn inter-as-vpn true")
    
    for port in range(1, 5):
        cfg.append(f"set / interface ethernet-1/{port} admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv4 admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv4 unnumbered admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv4 unnumbered interface system0.0")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv6 admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv6 router-advertisement router-role admin-state enable")
        cfg.append(f"set / network-instance default interface ethernet-1/{port}.0")
        
        cfg.append(f"set / network-instance default protocols bgp dynamic-neighbors interface ethernet-1/{port}.0 peer-group leaves")
        cfg.append(f"set / network-instance default protocols bgp dynamic-neighbors interface ethernet-1/{port}.0 allowed-peer-as [ 65001..65004 ]")

    cfg.append(f"set / network-instance default ip-forwarding receive-ipv4-check false")
    cfg.append(f"set / network-instance default ip-forwarding receive-ipv6-check false")
    cfg.append(f"set / routing-policy policy export-all default-action policy-result accept")
    cfg.append(f"set / routing-policy policy import-all default-action policy-result accept")
    cfg.append(f"set / network-instance default protocols bgp group leaves export-policy [ export-all ]")
    cfg.append(f"set / network-instance default protocols bgp group leaves import-policy [ import-all ]")
    return "\n".join(cfg)

def gen_leaf(leaf_id):
    cfg = []
    asn = 65000 + leaf_id
    ip_id = leaf_id
    
    cfg.append(f"set / interface system0 admin-state enable")
    cfg.append(f"set / interface system0 subinterface 0 admin-state enable")
    cfg.append(f"set / interface system0 subinterface 0 ipv4 admin-state enable")
    cfg.append(f"set / interface system0 subinterface 0 ipv4 address 100.0.0.{ip_id}/32")
    cfg.append(f"set / network-instance default protocols bgp admin-state enable")
    cfg.append(f"set / network-instance default protocols bgp router-id 100.0.0.{ip_id}")
    cfg.append(f"set / network-instance default protocols bgp autonomous-system {asn}")
    cfg.append(f"set / network-instance default type default")
    cfg.append(f"set / network-instance default interface system0.0")

    cfg.append(f"set / network-instance default protocols bgp group spine")
    cfg.append(f"set / network-instance default protocols bgp afi-safi ipv4-unicast admin-state enable")
    cfg.append(f"set / network-instance default protocols bgp afi-safi ipv6-unicast admin-state enable")
    cfg.append(f"set / network-instance default protocols bgp afi-safi evpn admin-state enable")
    cfg.append(f"set / network-instance default protocols bgp afi-safi ipv6-unicast ipv4-unicast advertise-ipv6-next-hops true")
    cfg.append(f"set / network-instance default protocols bgp afi-safi evpn evpn advertise-ipv6-next-hops true")
    # Spine interfaces
    for port in [31, 32]:
        cfg.append(f"set / interface ethernet-1/{port} admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv4 admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv4 unnumbered admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv4 unnumbered interface system0.0")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv6 admin-state enable")
        cfg.append(f"set / interface ethernet-1/{port} subinterface 0 ipv6 router-advertisement router-role admin-state enable")
        cfg.append(f"set / network-instance default interface ethernet-1/{port}.0")
        
        cfg.append(f"set / network-instance default protocols bgp dynamic-neighbors interface ethernet-1/{port}.0 peer-group spine")
        cfg.append(f"set / network-instance default protocols bgp dynamic-neighbors interface ethernet-1/{port}.0 allowed-peer-as [ 65000 ]")

    cfg.append(f"set / network-instance default ip-forwarding receive-ipv4-check false")
    cfg.append(f"set / network-instance default ip-forwarding receive-ipv6-check false")
    cfg.append(f"set / routing-policy policy export-all default-action policy-result accept")
    cfg.append(f"set / routing-policy policy import-all default-action policy-result accept")
    cfg.append(f"set / network-instance default protocols bgp group spine export-policy [ export-all ]")
    cfg.append(f"set / network-instance default protocols bgp group spine import-policy [ import-all ]")

    # MAC-VRF (L2 Domain)
    cfg.append(f"set / network-instance mac-vrf-1 type mac-vrf")
    cfg.append(f"set / network-instance mac-vrf-1 admin-state enable")
    cfg.append(f"set / tunnel-interface vxlan1 vxlan-interface 1 type bridged")
    cfg.append(f"set / tunnel-interface vxlan1 vxlan-interface 1 ingress vni 1")
    cfg.append(f"set / network-instance mac-vrf-1 vxlan-interface vxlan1.1")
    cfg.append(f"set / network-instance mac-vrf-1 protocols bgp-evpn bgp-instance 1 admin-state enable")
    cfg.append(f"set / network-instance mac-vrf-1 protocols bgp-evpn bgp-instance 1 vxlan-interface vxlan1.1")
    cfg.append(f"set / network-instance mac-vrf-1 protocols bgp-evpn bgp-instance 1 evi 1")
    cfg.append(f"set / network-instance mac-vrf-1 protocols bgp-evpn bgp-instance 1 ecmp 2")
    cfg.append(f"set / network-instance mac-vrf-1 protocols bgp-vpn bgp-instance 1 route-target export-rt target:1:1")
    cfg.append(f"set / network-instance mac-vrf-1 protocols bgp-vpn bgp-instance 1 route-target import-rt target:1:1")
    cfg.append(f"set / system network-instance protocols bgp-vpn bgp-instance 1")

    # Multi-homing definitions
    # Pair 1: leaf1 & leaf2, Pair 2: leaf3 & leaf4
    if leaf_id in [1, 2]:
        # Client 1 on eth-1/1
        cfg.append(f"set / interface ethernet-1/1 admin-state enable")
        cfg.append(f"set / interface ethernet-1/1 ethernet aggregate-id lag1")
        cfg.append(f"set / interface lag1 admin-state enable")
        cfg.append(f"set / interface lag1 vlan-tagging true")
        cfg.append(f"set / interface lag1 dynamic-subinterfaces vlan-range single-tagged low-vlan-id 1000 high-vlan-id 4000")
        cfg.append(f"set / interface lag1 subinterface 0 type bridged")
        cfg.append(f"set / interface lag1 subinterface 0 admin-state enable")
        cfg.append(f"set / interface lag1 subinterface 0 vlan encap untagged")
        cfg.append(f"set / interface lag1 lag lag-type lacp")
        cfg.append(f"set / interface lag1 lag lacp interval SLOW")
        cfg.append(f"set / interface lag1 lag lacp lacp-mode ACTIVE")
        cfg.append(f"set / interface lag1 lag lacp admin-key 11")
        cfg.append(f"set / interface lag1 lag lacp system-id-mac 00:00:00:00:00:11")
        cfg.append(f"set / interface lag1 lag lacp system-priority 11")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client1 admin-state enable")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client1 esi 01:24:24:24:24:24:24:00:00:11")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client1 multi-homing-mode all-active")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client1 interface lag1")
        cfg.append(f"set / network-instance mac-vrf-1 interface lag1.0")

        # Client 2 on eth-1/2
        cfg.append(f"set / interface ethernet-1/2 admin-state enable")
        cfg.append(f"set / interface ethernet-1/2 ethernet aggregate-id lag2")
        cfg.append(f"set / interface lag2 admin-state enable")
        cfg.append(f"set / interface lag2 vlan-tagging true")
        cfg.append(f"set / interface lag2 dynamic-subinterfaces vlan-range single-tagged low-vlan-id 1000 high-vlan-id 4000")
        cfg.append(f"set / interface lag2 subinterface 0 type bridged")
        cfg.append(f"set / interface lag2 subinterface 0 admin-state enable")
        cfg.append(f"set / interface lag2 subinterface 0 vlan encap untagged")
        cfg.append(f"set / interface lag2 lag lag-type lacp")
        cfg.append(f"set / interface lag2 lag lacp interval SLOW")
        cfg.append(f"set / interface lag2 lag lacp lacp-mode ACTIVE")
        cfg.append(f"set / interface lag2 lag lacp admin-key 12")
        cfg.append(f"set / interface lag2 lag lacp system-id-mac 00:00:00:00:00:12")
        cfg.append(f"set / interface lag2 lag lacp system-priority 12")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client2 admin-state enable")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client2 esi 01:24:24:24:24:24:24:00:00:12")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client2 multi-homing-mode all-active")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client2 interface lag2")
        cfg.append(f"set / network-instance mac-vrf-1 interface lag2.0")

        if leaf_id == 1:
            # Single-homed client 1 on eth-1/3
            cfg.append(f"set / interface ethernet-1/3 admin-state enable")
            cfg.append(f"set / interface ethernet-1/3 vlan-tagging true")
            cfg.append(f"set / interface ethernet-1/3 dynamic-subinterfaces vlan-range single-tagged low-vlan-id 1000 high-vlan-id 4000")
            cfg.append(f"set / interface ethernet-1/3 subinterface 0 type bridged")
            cfg.append(f"set / interface ethernet-1/3 subinterface 0 admin-state enable")
            cfg.append(f"set / interface ethernet-1/3 subinterface 0 vlan encap untagged")
            cfg.append(f"set / network-instance mac-vrf-1 interface ethernet-1/3.0")

    if leaf_id in [3, 4]:
        # Client 3 on eth-1/1
        cfg.append(f"set / interface ethernet-1/1 admin-state enable")
        cfg.append(f"set / interface ethernet-1/1 ethernet aggregate-id lag1")
        cfg.append(f"set / interface lag1 admin-state enable")
        cfg.append(f"set / interface lag1 vlan-tagging true")
        cfg.append(f"set / interface lag1 dynamic-subinterfaces vlan-range single-tagged low-vlan-id 1000 high-vlan-id 4000")
        cfg.append(f"set / interface lag1 subinterface 0 type bridged")
        cfg.append(f"set / interface lag1 subinterface 0 admin-state enable")
        cfg.append(f"set / interface lag1 subinterface 0 vlan encap untagged")
        cfg.append(f"set / interface lag1 lag lag-type lacp")
        cfg.append(f"set / interface lag1 lag lacp interval SLOW")
        cfg.append(f"set / interface lag1 lag lacp lacp-mode ACTIVE")
        cfg.append(f"set / interface lag1 lag lacp admin-key 13")
        cfg.append(f"set / interface lag1 lag lacp system-id-mac 00:00:00:00:00:13")
        cfg.append(f"set / interface lag1 lag lacp system-priority 13")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client3 admin-state enable")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client3 esi 01:24:24:24:24:24:24:00:00:13")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client3 multi-homing-mode all-active")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client3 interface lag1")
        cfg.append(f"set / network-instance mac-vrf-1 interface lag1.0")

        # Client 4 on eth-1/2
        cfg.append(f"set / interface ethernet-1/2 admin-state enable")
        cfg.append(f"set / interface ethernet-1/2 ethernet aggregate-id lag2")
        cfg.append(f"set / interface lag2 admin-state enable")
        cfg.append(f"set / interface lag2 vlan-tagging true")
        cfg.append(f"set / interface lag2 dynamic-subinterfaces vlan-range single-tagged low-vlan-id 1000 high-vlan-id 4000")
        cfg.append(f"set / interface lag2 subinterface 0 type bridged")
        cfg.append(f"set / interface lag2 subinterface 0 admin-state enable")
        cfg.append(f"set / interface lag2 subinterface 0 vlan encap untagged")
        cfg.append(f"set / interface lag2 lag lag-type lacp")
        cfg.append(f"set / interface lag2 lag lacp interval SLOW")
        cfg.append(f"set / interface lag2 lag lacp lacp-mode ACTIVE")
        cfg.append(f"set / interface lag2 lag lacp admin-key 14")
        cfg.append(f"set / interface lag2 lag lacp system-id-mac 00:00:00:00:00:14")
        cfg.append(f"set / interface lag2 lag lacp system-priority 14")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client4 admin-state enable")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client4 esi 01:24:24:24:24:24:24:00:00:14")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client4 multi-homing-mode all-active")
        cfg.append(f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment esi-client4 interface lag2")
        cfg.append(f"set / network-instance mac-vrf-1 interface lag2.0")

        if leaf_id == 3:
            # Single-homed client 3 on eth-1/3
            cfg.append(f"set / interface ethernet-1/3 admin-state enable")
            cfg.append(f"set / interface ethernet-1/3 vlan-tagging true")
            cfg.append(f"set / interface ethernet-1/3 dynamic-subinterfaces vlan-range single-tagged low-vlan-id 1000 high-vlan-id 4000")
            cfg.append(f"set / interface ethernet-1/3 subinterface 0 type bridged")
            cfg.append(f"set / interface ethernet-1/3 subinterface 0 admin-state enable")
            cfg.append(f"set / interface ethernet-1/3 subinterface 0 vlan encap untagged")
            cfg.append(f"set / network-instance mac-vrf-1 interface ethernet-1/3.0")

    return "\n".join(cfg)

if __name__ == '__main__':
    for i in range(1, 3):
        with open(f'/home/wds/github/srl-evpn-topo/configs/spine{i}.cfg', 'w') as f:
            f.write(gen_spine(i))
    
    for i in range(1, 5):
        with open(f'/home/wds/github/srl-evpn-topo/configs/leaf{i}.cfg', 'w') as f:
            f.write(gen_leaf(i))
