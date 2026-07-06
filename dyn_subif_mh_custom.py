#!/usr/bin/env python3
############################################################################
#
#   Filename:           dyn_subif_mh_custom.py
#
#   Description:        EVPN Multi-Homing: Dynamic Subinterfaces Event Handler
#                       creates/removes subinterfaces and their bindings
#                       into existing Network-Instances only,
#                       MAC-VRF, VXLAN, and EVPN must be pre-provisioned.
#
############################################################################
#
#              Copyright (c) 2026 Nokia
#
############################################################################

"""
Dynamic Subinterfaces – Multi-Homing (Pre-provisioned EVPN / MAC-VRF)

Customised copy of the shipped dynamic-subinterfaces-multi-homing.py, kept in
sync with the single-homed dyn_subif_custom.py where the two overlap. The only
customisation that applies here is the exclude-vlans option (see below): unlike
the single-homed handler, this one never creates a MAC-VRF, VXLAN-interface or
BGP-EVPN/BGP-VPN config, so it has no route-target to pin and therefore does NOT
carry the rt-asn option — the pre-provisioned MAC-VRFs own the route-targets.

This script is an event handler for the SR Linux event manager. It dynamically
creates and removes only subinterfaces and their bindings into existing
network-instances. All EVPN configuration, network-instances (MAC-VRFs), and
VXLAN interfaces must be pre-provisioned by the operator.

Use case
--------
In EVPN multi-homing, the same MAC-VRFs and subinterfaces must exist on all
leaf nodes attached to the same Ethernet Segment. Active VLANs on an interface
can be populated by:
- Data packets with that VLAN ID on the interface, or
- AD per EVI routes (route-target *:EVI, ESI matching the interface’s ES), or
- AD per ES routes (e.g. anycast MH) with matching route-target and ESI.

This script reacts to the active-vlans state (same input as single-homing
script) and only creates/removes:
- interface <name> subinterface <vlan_id>: type bridged, admin-state enable,
  vlan encap (single-tagged vlan-id or untagged)
- network-instance VLAN-<id> interface <name>.<vlan_id> (interface-ref)

It does NOT create or delete:
- network-instance VLAN-* (mac-vrf)
- tunnel-interface vxlan0 vxlan-interface *
- protocols bgp-evpn / bgp-vpn
- system network-instance protocols evpn ethernet-segments

Those must be pre-provisioned. Naming convention for pre-provisioned
network-instances is assumed to be VLAN-<id> and VLAN-untagged for vlan_id 0.

Input (in_json_str)
-------------------
JSON object with:
- paths: list of {"path": "<config path>", "value": [<vlan_id>, ...]}
  Path format: "interface <name> ..." (e.g. from active-vlan-detection).
  Value is the list of currently active VLAN IDs for that interface.
- options: dict, e.g. {"debug": "true"} to log add/remove decisions to stdout
- persistent-data: dict from the previous invocation (see Output)

Output
------
JSON object with:
- actions: list of {"set-cfg-path": {...}} or {"delete-cfg-path": {...}} or
  {"set-tools-path": {...}} to apply, and optionally
  {"reinvoke-with-delay": <ms>} when more work remains
- persistent-data: dict to pass back on the next run:
  - "interface": { "<interface_name>": [<vlan_id>, ...], ... }

Batching
--------
At most VLAN_BATCH_SIZE VLAN add/remove operations are applied per
invocation.  When the total pending work exceeds that limit the handler
appends {"reinvoke-with-delay": REINVOKE_DELAY_MS} so the framework calls
it again after the delay.  persistent-data is updated only for the operations
that were actually applied, so unprocessed VLANs remain as pending deltas on
the next invocation.

Options
-------
- debug="true": print messages when adding/removing VLANs or interfaces
- exclude-vlans="<list>": VLAN IDs to exclude from dynamic subinterface
  creation, even when reported as active within the interface's configured
  dynamic-subinterfaces vlan-range. Accepts a comma-separated list of individual
  IDs and/or ranges, e.g. "1000,1002,2000-2010". The token "untagged" (or 0)
  excludes the untagged subinterface. Excluded VLANs are never created; if one
  was previously created by this handler and is later added to the exclusion
  list, its subinterface (and network-instance binding) is torn down. Set this
  consistently with the single-homed handler so the same VLANs are suppressed
  fabric-wide.

Constants
---------
- NETWORK_INSTANCE_PREFIX: prefix for per-VLAN network-instance names ("VLAN-")

Notes
-----
- Pre-provision each MAC-VRF (network-instance VLAN-<id>), tunnel-interface
  vxlan0 vxlan-interface <id>, and EVPN/ES config as required for your MH setup.
- The handler can append a "system configuration save" action so changes
  persist across reboots; that block can be commented out to disable it.
"""

import json

# Maximum number of VLAN add/remove operations per invocation.  When the total
# pending work exceeds this limit the handler emits a "reinvoke-with-delay"
# action so the framework calls it again after the delay.
VLAN_BATCH_SIZE = 10
# Milliseconds to wait before the framework reinvokes this handler when there
# is still pending work.
REINVOKE_DELAY_MS = 100

# Prefix for per-VLAN mac-vrf network-instance names (must match pre-provisioned NIs)
NETWORK_INSTANCE_PREFIX = "VLAN-"


def _network_instance_name(vlan_id):
    """Return network-instance name for a VLAN ID (VLAN-untagged for 0, VLAN-<id> otherwise)."""
    return f"{NETWORK_INSTANCE_PREFIX}untagged" if vlan_id == 0 else f"{NETWORK_INSTANCE_PREFIX}{vlan_id}"


def _actions_add_subinterface(interface, vlan_id):
    """Return config actions to create subinterface and bind it to the network-instance.
    When vlan_id is 0, creates an untagged subinterface. Otherwise single-tagged."""
    name = _network_instance_name(vlan_id)
    actions = []
    if vlan_id == 0:
        actions.append({
            "set-cfg-path": {
                "path": f"interface {interface} subinterface {vlan_id} vlan encap untagged",
                "value": "",
            }
        })
    else:
        actions.append({
            "set-cfg-path": {
                "path": f"interface {interface} subinterface {vlan_id} vlan encap single-tagged vlan-id",
                "value": vlan_id,
            }
        })
    actions.extend([
        {
            "set-cfg-path": {
                "path": f"interface {interface} subinterface {vlan_id} type",
                "value": "bridged",
            }
        },
        {
            "set-cfg-path": {
                "path": f"interface {interface} subinterface {vlan_id} admin-state",
                "value": "enable",
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} interface {interface}.{vlan_id} interface-ref interface",
                "value": interface,
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} interface {interface}.{vlan_id} interface-ref subinterface",
                "value": vlan_id,
            }
        },
    ])
    return actions


def _actions_remove_subinterface(interface, vlan_id):
    """Return config actions to remove subinterface binding from network-instance and delete subinterface."""
    name = _network_instance_name(vlan_id)
    return [
        {
            "delete-cfg-path": {
                "path": f"network-instance {name} interface {interface}.{vlan_id}"
            }
        },
        {
            "delete-cfg-path": {
                "path": f"interface {interface} subinterface {vlan_id}"
            }
        },
    ]


def _get_interface(path):
    """
    Extracts the interface name from a configuration path string.
    Expected format: "interface <interface-name> ..."
    """
    words = path.split()
    if len(words) >= 2:
        return words[1]
    return None


def _parse_vlan(v):
    if v == "untagged":
        return 0
    return int(v)


def _parse_vlan_exclusions(options):
    """Parse the exclude-vlans option into a set of excluded VLAN IDs.

    Accepts individual VLAN IDs and/or inclusive ranges written with '-'
    (e.g. "1000,1002,2000-2010"). The event handler passes an option configured
    with a scalar `value` as a string and one configured with a `values`
    leaf-list as a JSON array, so both forms are supported: a string is split on
    commas, while a list is consumed element by element (each element may itself
    be an ID, a range, or "untagged"). The token "untagged" maps to VLAN ID 0.
    Blank/malformed tokens are ignored so a typo in one entry does not disable
    the whole handler. Returns an empty set when the option is absent or empty.

    Kept identical to the single-homed dyn_subif_custom.py implementation so a
    given exclude-vlans value suppresses the same VLANs on single-homed and
    multi-homed interfaces alike.
    """
    raw = options.get("exclude-vlans")
    if not raw:
        return set()
    if isinstance(raw, (list, tuple)):
        tokens = raw
    else:
        tokens = str(raw).split(",")
    excluded = set()
    for part in tokens:
        part = str(part).strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                continue
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                continue
            for vlan_id in range(start, end + 1):
                excluded.add(vlan_id)
        else:
            try:
                excluded.add(_parse_vlan(part))
            except ValueError:
                continue
    return excluded


def event_handler_main(in_json_str):
    """
    Main event handler for dynamic subinterfaces in multi-homing mode.

    Only creates/removes subinterfaces and network-instance interface bindings.
    MAC-VRFs, VXLAN interfaces, and EVPN must be pre-provisioned.
    """
    in_json = json.loads(in_json_str)
    paths = in_json.get("paths", [])
    options = in_json.get("options", {})
    persist = in_json.get("persistent-data", {})

    response_actions = []

    # VLANs the operator has excluded from dynamic creation. These are dropped
    # from the desired set below, so they are never instantiated even when
    # reported active within the interface's configured vlan-range. A VLAN moved
    # into the exclusion list after being created is treated as no-longer-desired
    # and torn down by the normal remove path.
    excluded_vlans = _parse_vlan_exclusions(options)

    # Snapshot of per-interface VLAN sets from the previous run (mutable working copy)
    previous_interface_vlans = {iface: set(vlans)
                                 for iface, vlans in persist.get("interface", {}).items()}

    # Desired VLAN sets reported by the event system (current active VLANs per interface)
    desired_interface_vlans = {}
    for path in paths:
        if not path or "path" not in path or "value" not in path:
            continue
        interface = _get_interface(path["path"])
        if interface is None:
            continue
        active = set(_parse_vlan(v) for v in path["value"])
        if excluded_vlans:
            skipped = active & excluded_vlans
            if skipped and options.get("debug") == "true":
                print(f"Excluding VLAN(s) {sorted(skipped)} on interface {interface} "
                      f"(exclude-vlans option)")
            active -= excluded_vlans
        desired_interface_vlans[interface] = active

    # Build the pending (interface, vlan_id, action) list, but stop collecting as soon as
    # VLAN_BATCH_SIZE + 1 entries are found.  We only need one extra entry to know whether
    # a reinvoke is required; building the full list for thousands of VLANs would exhaust
    # micropython's limited heap before any work is done.
    pending_changes = []
    _batch_limit = VLAN_BATCH_SIZE + 1

    for interface, current_vlans in desired_interface_vlans.items():
        previous_vlans = previous_interface_vlans.get(interface, set())
        for vlan_id in sorted(current_vlans - previous_vlans):
            pending_changes.append((interface, vlan_id, "add"))
            if len(pending_changes) >= _batch_limit:
                break
        if len(pending_changes) < _batch_limit:
            for vlan_id in sorted(previous_vlans - current_vlans):
                pending_changes.append((interface, vlan_id, "remove"))
                if len(pending_changes) >= _batch_limit:
                    break
        if len(pending_changes) >= _batch_limit:
            break

    # Interfaces that existed in persist but are no longer reported: remove all VLANs
    if len(pending_changes) < _batch_limit:
        for old_interface, old_vlans in previous_interface_vlans.items():
            if old_interface not in desired_interface_vlans:
                if options.get("debug") == "true":
                    print(f"Interface {old_interface} no longer active, removing all VLANs")
                for vlan_id in sorted(old_vlans):
                    pending_changes.append((old_interface, vlan_id, "remove"))
                    if len(pending_changes) >= _batch_limit:
                        break
            if len(pending_changes) >= _batch_limit:
                break

    # Limit work to VLAN_BATCH_SIZE per invocation; schedule a reinvoke for the rest
    needs_reinvoke = len(pending_changes) > VLAN_BATCH_SIZE
    batch = pending_changes[:VLAN_BATCH_SIZE]

    # Working copy of the interface->vlan mapping; updated only for the changes we
    # actually apply.  Unapplied changes remain visible as deltas on the next run
    # because persistent-data will still reflect the pre-change state for them.
    result_interface_vlans = {iface: set(vlans)
                               for iface, vlans in previous_interface_vlans.items()}

    for interface, vlan_id, action in batch:
        if action == "add":
            if options.get("debug") == "true":
                print(f"Adding VLAN {vlan_id} for interface {interface}")
            response_actions.extend(_actions_add_subinterface(interface, vlan_id))
            result_interface_vlans.setdefault(interface, set()).add(vlan_id)
        else:  # remove
            if options.get("debug") == "true":
                print(f"Deleting VLAN {vlan_id} for interface {interface}")
            response_actions.extend(_actions_remove_subinterface(interface, vlan_id))
            if interface in result_interface_vlans:
                result_interface_vlans[interface].discard(vlan_id)

    # Drop empty interface entries so they are no longer tracked
    result_interface_vlans = {iface: vlans
                               for iface, vlans in result_interface_vlans.items() if vlans}

    if needs_reinvoke:
        response_actions.append({"reinvoke-with-delay": REINVOKE_DELAY_MS})

    response = {
        "actions": response_actions,
        "persistent-data": {
            "interface": {iface: sorted(vlans) for iface, vlans in result_interface_vlans.items()},
        },
    }

    # Uncomment to persist configuration across reboots and node failures
    # response["actions"].append(
    #     {
    #         "set-tools-path": {
    #             "path": "system configuration save",
    #             "value": ""
    #         }
    #     }
    # )

    return json.dumps(response)
