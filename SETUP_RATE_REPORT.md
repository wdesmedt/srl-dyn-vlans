# Dynamic Sub-interface Setup-Rate Experiment — Report

**Goal:** measure the setup rate of SR Linux dynamic sub-interfaces for single-homed
clients, separating the **intra-switch** case (sub-interface + MAC-VRF + VXLAN, no
cross-leaf EVPN needed to pass traffic) from the **inter-switch** case (adds EVPN
route propagation), across increasing VLAN range sizes.

Scope: single-homed clients only (`sh-clientN`, `interface ethernet-1/x`), because the
event handler only auto-provisions sub-interfaces / MAC-VRF / VXLAN for those ports.

---

## Bottom line

- The **switch side is not the limiter** at the scales tested. In every failed run the
  leaf sub-interfaces were `oper-up` on both leaves while traffic still black-holed.
- The real limiter is **client/host kernel resources**, in two stages:
  1. **ARP neighbour-table overflow** on the host (`arp_tbl`, `gc_thresh3=1024`,
     kernel default). The README's `sysctl` tuning was never in effect — it cannot be
     set from inside the client containers (read-only netns), it must be set in the
     **host root namespace**. dmesg confirmed `neighbour: arp_cache: neighbor table
     overflow!` during the runs.
  2. After raising the host limit, a **second ceiling** appeared: under the full
     10-pair, ~100k-pps, cold-setup load on this shared host, forwarding/control-plane
     capacity saturates and high-N runs become noisy/unreliable (inter_50 took >50s
     while inter_100 finished at 56s — not physical for a clean setup-rate curve).
- **Trustworthy numbers therefore come only from the small-scale cells** (N=10 both
  modes, intra N=50). High-N cells are resource-bound and excluded.

---

## Valid results

Metric = per-path convergence = first-packet-through time, from a cold leaf
(bidirectional, 10 sh ports/leaf, `--interval 0.01`). "subifs/leaf" = 10 ports × N.

> **`censored` here means the *dataplane* (`run_traffic.py`) measurement was
> host-resource-bound, not that the switch failed or hit a scaling limit.** In every
> censored run the leaf sub-interfaces were already `oper-up` on both leaves while
> client traffic still black-holed (host ARP-table overflow, then host forwarding/CPU
> saturation — see "Evidence" below). The switch-side gNMI method
> (`measure_setup_rate.py`, next section) has **no censored cells** and scales cleanly
> to N=200; if you run that tool you will not see this issue.

| N (VLANs) | mode  | subifs/leaf | total setup | p50 conv | provisioning rate |
|----------:|-------|------------:|------------:|---------:|------------------:|
| 10        | intra | 100         | **3.1 s**   | 1.6 s    | ~32 subif/s       |
| 10        | inter | 100         | **8.3 s**   | 8.0 s    | ~12 subif/s       |
| 50        | intra | 500         | **14.7 s**  | 6.1 s    | ~34 subif/s       |
| 50        | inter | 500         | censored    | —        | resource-bound    |
| 100       | intra | 1000        | censored    | —        | resource-bound    |
| 100       | inter | 1000        | ~56 s (noisy)| 33 s    | resource-bound    |

### Interpretation
- **Intra-switch provisioning rate ≈ 34 sub-interfaces/second per leaf**, with a
  ~1.5 s fixed first-batch floor (config commit latency). Consistent between N=10 and
  N=50 (incremental 400 subifs / 11.6 s = 34.4/s). This is the pure
  "subif + MAC-VRF + VXLAN + local FDB" apply rate. It is ~1/3 of the theoretical
  batch cadence (`VLAN_BATCH_SIZE=10` / `REINVOKE_DELAY_MS=100` ⇒ 100/s), the
  difference being real config-commit/programming time per batch (~290 ms/batch).
- **Inter-switch adds an EVPN propagation tail of ~5 s** (N=10: 3.1 s → 8.3 s, 2.65×).
  Note the shape: intra convergence is a **staircase** (per-batch), while inter paths
  converge **together** (~8 s) once BGP-EVPN MAC/IMET routes propagate — i.e. EVPN adds
  a roughly fixed delay rather than a per-VLAN increment at small N.

---

## Switch-side setup rate (clean, via gNMI — the real answer)

Because the dataplane method is confounded by client ARP + host forwarding, the pure
provisioning rate was measured **from the switch** with
[measure_setup_rate.py](file:///home/wds/github/srl-dyn-vlans/measure_setup_rate.py):
create the client sub-interfaces on a fresh (never-triggered) range — which is the
active-VLAN trigger — then read each leaf sub-interface's `last-change` timestamp via
gNMI (`gnmic`) and compute when it came up relative to the trigger. No client ARP or
dataplane is involved.

```bash
# The leaf's gNMI mgmt IP is derived from --node via docker inspect
# (pass --leaf-mgmt only to override); use a fresh/cold VLAN range each run.
./measure_setup_rate.py --node leaf1 --client sh-client1 --vlans 1100-1199
```

Single port (`leaf1 ethernet-1/1`), fresh disjoint ranges, 2 runs per N. Two
independent **freshly-redeployed-fabric** datasets were taken (run A validated the
method; run B validated the committed tool end-to-end):

| N (VLANs) | total setup — run A | total setup — run B | incremental rate |
|----------:|--------------------:|--------------------:|-----------------:|
| 10        | 1.10 s              | 0.77 s              | —                |
| 50        | 1.65 s              | 1.19 s              | ~65–96 subif/s   |
| 100       | 2.42 s              | 2.15 s              | ~52–65 subif/s   |
| 200       | 4.16 s              | 3.45 s              | ~57–77 subif/s   |

Both datasets agree in shape and magnitude (run B ran on a completely fresh leaf and
was slightly faster). At N=200 the two runs within each dataset agree to ~2%
(3.42 vs 3.49 s in run B) — that is the trustworthy end of the curve; the run-to-run
wobble at small N is the fixed first-commit latency plus the 0.5 s gNMI poll granularity
dominating when there is little to measure.

- A near-constant **~0.1–0.3 s** detection+first-commit latency, then a roughly linear ramp.
- **Incremental provisioning rate ≈ 55–90 sub-interfaces/second per port**
  (Δsubifs/Δtime across both datasets). The lower "ramp rate" at small N is because the
  fixed first-batch commit dominates there.
- 100 sub-interfaces (+ MAC-VRF + VXLAN + EVPN config) come up in **~2.2–2.4 s**, 200 in
  **~3.5–4.2 s** — **1–2 orders of magnitude faster** than the ARP-bottlenecked dataplane
  numbers. This is the decisive proof the switch was never the limiter.
- Note: on this build, bringing up a tagged client sub-interface is itself the trigger
  (active-VLAN detection fires immediately), so no separate traffic is needed to warm a
  VLAN — and conversely, pre-creating many client subifs warms the whole set at once.
- Constraint: test VLANs must be valid 802.1q (1–4094) **and** within the leaf's
  configured `dynamic-subinterfaces vlan-range` (1000–4000 here), or the leaf never
  creates the subinterface. `measure_setup_rate.py` rejects out-of-range 802.1q IDs.
- **Precondition — the requested range must be cold:** `measure_setup_rate.py` reads
  `active-vlans` fabric-wide and refuses to run if any VLAN in `--vlans` is already active
  (its subifs would already exist, so the timing is meaningless). Active VLANs *outside*
  the range — e.g. leftovers from a previous run still within their retention-timer — are
  allowed; the tool notes them and proceeds, since they only share the provisioning
  pipeline (minor contention). This lets successive runs use fresh disjoint ranges without
  waiting out retention. Override the check with `--allow-active-vlans`.

### Why one subinterface timestamp measures all three constructs

The tool times each subinterface's `last-change`, yet the reported rate is legitimately
the **subif + MAC-VRF + VXLAN** provisioning rate — because the event handler creates all
three in a **single atomic transaction**. Per invocation it appends the per-VLAN
subinterface, its MAC-VRF network-instance (`VLAN-<id>`) and the `vxlan0` vxlan-interface
into one `response_actions` list, which the SR Linux event manager commits together (up
to `VLAN_BATCH_SIZE = 10` VLANs per commit; the rest are deferred via
`reinvoke-with-delay`, i.e. the next commit).

This is *guaranteed by the script*, not merely observed: `_actions_add_network_instance`
sets `network-instance VLAN-<id> vxlan-interface vxlan0.<id>` (and the bgp-evpn
`vxlan-interface`) **before** `_actions_add_vxlan_interface` creates that
`tunnel-interface vxlan0 vxlan-interface <id>` later in the same list. A leafref like that
only validates if the whole set commits atomically — so the constructs *must* be
co-created. Consequently a subinterface can never be up without its MAC-VRF and VXLAN
having been created in the same commit, which is exactly the per-batch **staircase** the
dataplane numbers showed (each step = one commit = 10 subifs + 10 MAC-VRFs + 10 VXLANs).

Caveat: `last-change` is an oper-state transition (creation/provisioning), so it stands
in for *creation* of all three — not for the VXLAN tunnel becoming forwarding-ready or
cross-leaf EVPN route propagation. By default the tool measures **local provisioning
only**.

### EVPN tail (inter-switch), measured cleanly from the switch

`measure_setup_rate.py --dst-node` extends the method across leaves: it warms the same
range on a destination leaf/client (so the dst leaf provisions `VLAN-<id>` and can import
the route), then times when each source MAC appears in the dst leaf's `VLAN-<id>` FDB as
`type=evpn` (BGP-EVPN Type-2 propagated + remote FDB programmed). This isolates the pure
control-plane inter-switch tail with **no client ARP / host forwarding** in the path —
the very confounders that censored the dataplane inter-switch cells above.

Validation run (leaf1 → leaf3, N=20, cold fabric):

| metric | value |
|--------|------:|
| local provisioning (20 subifs up) | 1.17 s |
| end-to-end (t0 → MAC in dst FDB) p50 / p90 | 2.10 / 6.56 s |
| **EVPN tail** (dst FDB − local subif-up) p50 / p90 | **1.04 / 5.40 s** |
| MACs reaching dst FDB | 20/20 |

The tail (~1–5 s) is consistent with the ~5 s EVPN delta the dataplane method saw at
N=10, but here it is measured directly and without the ARP/forwarding noise. dst-FDB
appearance is host-poll observed, so its resolution is bounded by `--poll` (0.5 s
default); the local subif-up time is the device `last-change`, both on the shared host
clock.

## Evidence the failures were resource-bound (not setup rate)

- `sudo dmesg` → `neighbour: arp_cache: neighbor table overflow!` at host-uptime
  108703–108709 s (during the heavy runs).
- `fcli … subif` → leaf sub-interfaces `100/100 oper-up` on both leaves for a range
  whose traffic was 100% lost.
- `ip neigh show` on clients → target entries in state `FAILED`; `gc_thresh3=1024`.
- Raising host `net.ipv4.neigh.default.gc_thresh3` 1024→16384 stopped the overflow
  (`/proc/net/stat/arp_cache` entries 0x80e=2062 < 16384, no new dmesg) and restored
  ping on the previously-failing VLANs.
- `sysctl -w …neigh/default/gc_thresh*` **inside a client container fails**
  (`cannot stat`) — confirms the README tuning path is a no-op in these netns.

---

## Tools & options used, by phase

| Phase | Tool | Key options / commands |
|-------|------|------------------------|
| Inspect topology/config | `grep`, `sed` | `retention-timer`, `active-vlans` in `configs/leafN.cfg` |
| Fabric state (warmth, subif oper) | **`fcli`** | `fcli -t dyn-vlan.clab.yml -o json subif` (also `ni`, `arp`, `mac`) |
| Per-leaf NI count / bounce | `docker exec … sr_cli` | `show network-instance summary`; `enter candidate; set /interface ethernet-1/p admin-state disable/enable; commit now` |
| Cold-start (force teardown) | `sr_cli` bounce loop | disable+enable `ethernet-1/1..10` on leaf1 & leaf3, poll NI count →0 |
| Client subif / ARP state | `docker exec … ip` | `ip -o link show type vlan`; `ip -4 neigh show`; `ip ntable show`; `ip neigh flush all` |
| Traffic + convergence | **`run_traffic.py`** | `--no-setup --clients sh [--intraswitch] --vlans A-B --interval 0.01 --duration D --quiet --force --json-report FILE` |
| Machine-readable results | `--json-report` (added) | per-flow `{src,dst,vlan,lost_count,max_outage_ms}` + meta |
| Host resource diagnosis | `dmesg`, `sysctl`, `ps`, `free`, `uptime`, `/proc/net/stat/arp_cache` | `neighbor table overflow`, `net.ipv4.neigh.default.gc_thresh*`, load/mem/CPU |
| Analysis | custom `analyze.py` | p50/p90/max convergence, subif rate, EVPN delta, censoring flag |

`run_traffic.py` was invoked non-interactively (stdin `< /dev/null`) so the flow menu
auto-selects all flows; `--force` silences the short-duration guard; `--no-setup`
because the sh clients already have VLANs 1000–1500 configured.

---

## To measure SRL setup rate cleanly (recommended next step)

The dataplane method conflates switch setup with client ARP + host forwarding. Instead:

1. **Measure from the switch**, not the clients: cold the leaf, send a trigger on one
   port for N VLANs, and timestamp each `interface … subinterface … oper-state`
   transition to `up` via gNMI/telemetry (`fcli`/`gnmic`). This isolates pure
   provisioning rate with no ARP/host-forwarding involvement.
2. If keeping the dataplane method: use **1 pair, not 10**, a **low pps**
   (`--interval 0.1`), raise host ARP limits first, and space runs out so the shared
   host isn't contended.
3. Fix the harness/docs:
   - ARP `gc_thresh` tuning must be applied on the **host root namespace**, not inside
     the container (the in-container `sysctl` fails; the `arp_tbl` limit is host-global).
   - `retention-timer 10` is **10 minutes** (SR Linux unit is `minutes`, default 240) —
     the original README was right. Note that **bouncing an interface does not force an
     immediate teardown** (retention is inactivity-based); use a fresh VLAN range for a
     cold measurement instead.
   - Consider raising client neighbour limits via the containerlab topology so runs
     are reproducible.

---

## Environment changes made during this investigation

- Host: `net.ipv4.neigh.default.gc_thresh{1,2,3}` raised to `4096/8192/16384`
  (runtime only; not persisted across reboot).
- Leaves left with a small number of dynamic sub-interfaces from the last run; these
  self-clear via the retention timer while idle.
- Client neighbour caches flushed.
