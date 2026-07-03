#!/usr/bin/env python3
############################################################################
#
#   Filename:           dynamic-subinterfaces.py
#
#   Description:        Dynamic subinterface event handler for active VLAN
#                       detection and management
#
############################################################################
#
#              Copyright (c) 2026 Nokia
#
############################################################################

"""
Dynamic Subinterface Event Handler

This script is an event handler for the SR Linux event manager. It dynamically
creates and manages subinterfaces, network-instances, and EVPN/VXLAN resources
based on active VLAN detection (see active-vlan-detection in the dynamic
subinterfaces YANG model).

Overview
--------
When VLANs are detected as active on an interface, it does the following:
- Creates subinterfaces for newly detected, single-tagged VLANs
- Creates a per-VLAN mac-vrf network-instance named "VLAN-<id>"
- Associates the subinterfaces with their corresponding network-instances
- Creates VXLAN tunnel interfaces and configures BGP-EVPN/BGP-VPN for each VLAN
- Enables both the subinterfaces and their network-instances

When VLANs are no longer active (or the interface is removed), it removes
subinterfaces, network-instance bindings, and tears down the VLAN’s
network-instance and VXLAN/EVPN when the last reference is gone.

State is persisted across invocations so the handler can compute add/remove
deltas correctly.

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
  - "network-instance": [<vlan_id>, ...]  # VLANs that have a network-instance

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
- exclude-vlans="<list>": VLAN IDs to exclude from dynamic creation, even when
  reported as active within the interface's configured dynamic-subinterfaces
  vlan-range. Accepts a comma-separated list of individual IDs and/or ranges,
  e.g. "1000,1002,2000-2010". The token "untagged" (or 0) excludes the untagged
  subinterface. Excluded VLANs are never created; if one was previously created
  by this handler and is later added to the exclusion list, it is torn down.
- rt-asn="<asn>": administrator field for each mac-vrf's import/export
  route-target (target:<rt-asn>:<evi>). Defaults to RT_ASN_DEFAULT. Set the SAME
  value on every leaf: with an eBGP overlay each leaf has a different local AS,
  so the default auto-derived RT (local-AS:EVI) differs per leaf and EVPN routes
  are never imported across leaves. A static ASN here keeps the RT consistent.

Constants
---------
- NETWORK_INSTANCE_PREFIX: prefix for per-VLAN network-instance names ("VLAN-")
- ECMP_VALUE: ECMP value set on BGP-EVPN bgp-instance (default 8)

Notes
-----
- Network-instances and VXLAN/EVPN resources are created when the first
  subinterface for a VLAN is added (across any interface) and deleted when
  the last subinterface for that VLAN is removed.
- The handler can append a "system configuration save" action so changes
  persist across reboots; that block can be commented out to disable it.

Reserved naming and ownership
-----------------------------
- Network-instance names "VLAN-<id>" and "VLAN-untagged" (id 0), and
  tunnel-interface vxlan0 vxlan-interface <id>, are reserved for this script.
  Do not create manual config using these names; they may be deleted when
  the script tears down resources.
- Created network-instances are tagged with a description (OWNERSHIP_DESCRIPTION)
  so operators can identify script-managed resources. Deletes are only emitted
  for VLANs tracked in persistent-data (i.e. previously created by this script).
"""

import json

# Maximum number of VLAN add/remove operations per invocation.  When the total
# pending work exceeds this limit the handler emits a "reinvoke-with-delay"
# action so the remainder is processed in subsequent invocations.
VLAN_BATCH_SIZE = 10
# Milliseconds to wait before the framework reinvokes this handler when there
# is still pending work.
REINVOKE_DELAY_MS = 100

# Prefix for per-VLAN mac-vrf network-instance names (e.g. "VLAN-100", "VLAN-untagged" for vlan_id 0)
NETWORK_INSTANCE_PREFIX = "VLAN-"
# ECMP value applied to network-instance protocols bgp-evpn bgp-instance 1
ECMP_VALUE = 8
# Untagged (vlan_id 0): use reserved EVI/VNI to avoid collision with VLAN IDs 1..4094
UNTAGGED_EVI = 4096   # BGP EVPN evi for network-instance
UNTAGGED_VNI = 4096   # VXLAN ingress vni for tunnel-interface
# Ownership marker set on network-instances we create; reserved naming (VLAN-<id>, vxlan0.<id>) is for this script only
OWNERSHIP_DESCRIPTION = "Managed by dynamic-subinterfaces (active VLAN detection)"
# Default administrator field for the import/export route-targets (target:<RT_ASN>:<evi>).
# Static and ASN-independent so every leaf derives the SAME RT; overridable via the
# "rt-asn" option. Needed because the eBGP overlay gives each leaf a different local
# AS, so the auto-derived RT (local-AS:EVI) would otherwise differ per leaf.
RT_ASN_DEFAULT = 65535


def _network_instance_name(vlan_id):
    """Return network-instance name for a VLAN ID (VLAN-untagged for 0, VLAN-<id> otherwise)."""
    return f"{NETWORK_INSTANCE_PREFIX}untagged" if vlan_id == 0 else f"{NETWORK_INSTANCE_PREFIX}{vlan_id}"


def _actions_add_network_instance(vlan_id, rt_asn):
    """
    Return config actions to create a mac-vrf network-instance with
    vxlan-interface, bgp-vpn, and bgp-evpn.

    rt_asn is the administrator field of the import/export route-targets. It must
    be identical on every leaf, so it is a static, ASN-independent value rather
    than the switch's local BGP AS: in an eBGP overlay each leaf has a different
    AS, and the auto-derived route-target (local-AS:EVI) therefore differs per
    leaf, so imports never match and EVPN routes are not exchanged. Pinning the
    RT to <rt_asn>:<EVI> makes it consistent fabric-wide.
    """
    name = _network_instance_name(vlan_id)
    vxlan_if_name = f"vxlan0.{vlan_id}"
    if vlan_id == 0:
        evi = UNTAGGED_EVI
    else:
        evi = vlan_id
    route_target = f"target:{rt_asn}:{evi}"
    return [
        {
            "set-cfg-path": {
                "path": f"network-instance {name} type",
                "value": "mac-vrf"
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} admin-state",
                "value": "enable"
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} description",
                "value": OWNERSHIP_DESCRIPTION
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} vxlan-interface {vxlan_if_name}",
                "value": ""
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} protocols bgp-vpn bgp-instance 1 route-target export-rt",
                "value": route_target
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} protocols bgp-vpn bgp-instance 1 route-target import-rt",
                "value": route_target
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} protocols bgp-evpn bgp-instance 1 admin-state",
                "value": "enable"
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} protocols bgp-evpn bgp-instance 1 vxlan-interface",
                "value": vxlan_if_name
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} protocols bgp-evpn bgp-instance 1 evi",
                "value": evi
            }
        },
        {
            "set-cfg-path": {
                "path": f"network-instance {name} protocols bgp-evpn bgp-instance 1 ecmp",
                "value": str(ECMP_VALUE)
            }
        },
    ]


def _actions_add_vxlan_interface(vlan_id):
    """Return config actions to create tunnel-interface vxlan0 vxlan-interface for a VLAN."""
    vni = vlan_id if vlan_id != 0 else UNTAGGED_VNI
    return [
        {
            "set-cfg-path": {
                "path": f"tunnel-interface vxlan0 vxlan-interface {vlan_id} type",
                "value": "bridged",
            }
        },
        {
            "set-cfg-path": {
                "path": f"tunnel-interface vxlan0 vxlan-interface {vlan_id} ingress vni",
                "value": vni,
            }
        },
    ]


def _actions_add_subinterface(interface, vlan_id):
    """Return config actions to create subinterface and bind it to the network-instance.
    When vlan_id is 0, creates an untagged subinterface (no vlan encap). Otherwise single-tagged."""
    name = _network_instance_name(vlan_id)
    actions = []
    if vlan_id == 0:
        # Untagged subinterface: use vlan encap untagged (no vlan-id)
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


def _actions_remove_network_instance_and_vxlan(vlan_id):
    """Return config actions to remove VXLAN interface, BGP-EVPN/BGP-VPN, and network-instance for a VLAN."""
    name = _network_instance_name(vlan_id)
    vxlan_if_name = f"vxlan0.{vlan_id}"
    return [
        {
            "delete-cfg-path": {
                "path": f"tunnel-interface vxlan0 vxlan-interface {vlan_id}"
            }
        },
        {
            "delete-cfg-path": {
                "path": f"network-instance {name} protocols bgp-evpn bgp-instance 1"
            }
        },
        {
            "delete-cfg-path": {
                "path": f"network-instance {name} protocols bgp-vpn bgp-instance 1"
            }
        },
        {
            "delete-cfg-path": {
                "path": f"network-instance {name} vxlan-interface {vxlan_if_name}"
            }
        },
        {
            "delete-cfg-path": {
                "path": f"network-instance {name}"
            }
        },
    ]

# Get interface name from the path string


def _get_interface(path):
    """
    Extracts the interface name from a configuration path string.

    Expected format: "interface <interface-name> ..."

    Args:
        path: Configuration path string

    Returns:
        Interface name (second word in path) or None if not found
    """
    words = path.split()
    if len(words) >= 2:
        return words[1]

    return None


def _parse_vlan(v):
    if v == "untagged":
        return 0
    return int(v)


def _parse_rt_asn(options):
    """Return the route-target administrator ASN from the rt-asn option.

    Accepts a scalar option value (or the first element of a values leaf-list).
    Falls back to RT_ASN_DEFAULT when the option is absent or not a valid integer,
    so a typo cannot break provisioning.
    """
    raw = options.get("rt-asn")
    if raw is None or raw == "":
        return RT_ASN_DEFAULT
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return RT_ASN_DEFAULT


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


# Main entry function for event handler


def event_handler_main(in_json_str):
    """
    Main event handler function called by the SR Linux event handler framework.

    Processes active VLAN changes and generates configuration actions to:
    - Create subinterfaces for new active VLANs
    - Configure VLAN encapsulation (single-tagged)
    - Enable newly created subinterfaces
    - Create network-instances for new active VLANs
    - Create VXLAN interfaces for new active VLANs
    - Create BGP-EVPN/BGP-VPN for new active VLANs
    - Remove subinterfaces for VLANs that are no longer active
    - Remove network-instances for VLANs that are no longer active
    - Remove VXLAN interfaces for VLANs that are no longer active
    - Remove BGP-EVPN/BGP-VPN for VLANs that are no longer active


    Args:
        in_json_str: JSON string containing:
            - paths: List of configuration paths with values (active VLANs)
            - options: Handler options (e.g., debug mode)
            - persistent-data: Previous state from last handler invocation

    Returns:
        JSON string containing:
            - actions: List of configuration actions to perform
            - persistent-data: Updated state to persist for next invocation
    """
    # Parse input json string passed by event handler
    in_json = json.loads(in_json_str)
    paths = in_json.get("paths", [])  # Configuration paths with VLAN values
    options = in_json.get("options", {})  # Handler options (debug, etc.)
    persist = in_json.get('persistent-data', {})  # Previous state

    # Route-target administrator ASN (static, consistent fabric-wide) used when
    # creating each mac-vrf's import/export RT as target:<rt_asn>:<evi>.
    rt_asn = _parse_rt_asn(options)

    response_actions = []  # List of config actions to return

    # Snapshot of per-interface VLAN sets from the previous run (mutable working copy)
    previous_interface_vlans = {iface: set(vlans)
                                 for iface, vlans in persist.get("interface", {}).items()}
    previous_network_instances = set(persist.get("network-instance", []))

    # VLANs the operator has excluded from dynamic creation. These are dropped
    # from the desired set below, so they are never instantiated even when
    # reported active within the interface's configured vlan-range. A VLAN moved
    # into the exclusion list after being created is treated as no-longer-desired
    # and torn down by the normal remove path.
    excluded_vlans = _parse_vlan_exclusions(options)

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

    # Track newly created network-instances so we can update the NI set correctly
    added_netinst_vlans = set()

    for interface, vlan_id, action in batch:
        if action == "add":
            if options.get("debug") == "true":
                print(f"Adding VLAN {vlan_id} for interface {interface}")
            # First reference to this VLAN across any interface: create NI + VXLAN/EVPN
            if vlan_id not in previous_network_instances and vlan_id not in added_netinst_vlans:
                response_actions.extend(_actions_add_network_instance(vlan_id, rt_asn))
                response_actions.extend(_actions_add_vxlan_interface(vlan_id))
                added_netinst_vlans.add(vlan_id)
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

    # VLANs that still have at least one subinterface after this batch
    current_network_instances = set()
    for vlans in result_interface_vlans.values():
        current_network_instances |= vlans

    # Delete network-instance + VXLAN only for VLANs we previously created whose
    # last subinterface was removed in this batch (still not present after processing).
    for vlan_id in previous_network_instances:
        if vlan_id not in current_network_instances:
            response_actions.extend(_actions_remove_network_instance_and_vxlan(vlan_id))

    # Build the response with actions and persistent data
    response_persistent_data = {
        "interface": {iface: sorted(vlans) for iface, vlans in result_interface_vlans.items()},
        "network-instance": sorted(current_network_instances),
    }
    if needs_reinvoke:
        response_actions.append({"reinvoke-with-delay": REINVOKE_DELAY_MS})

    response = {'actions': response_actions, 'persistent-data': response_persistent_data}

    # Uncomment the following section to enable automatic save to startup config
    # This ensures configuration persists across reboots and node failures

    # response_actions.append(
    #     {
    #         "set-tools-path": {
    #             "path": "system configuration save",
    #             "value": ""
    #         }
    #     }
    # )

    # Return JSON response to event handler framework
    return json.dumps(response)
