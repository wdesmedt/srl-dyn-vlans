#!/usr/bin/env python3
"""
Transport helpers for talking to SR Linux nodes from the host tools, over the node's
management plane rather than docker — so the tools run against real hardware.

Two channels, both keyed by a node *name*:
  - gNMI (via `gnmic`) for reads — state/config GET.
  - SSH (via `ssh` + `sshpass`) into `sr_cli` for config changes and `tools` actions.

Node name -> management host/IP resolution (no docker):
  1. env `SRL_ADDR_<node>` if set (e.g. SRL_ADDR_leaf1=10.0.0.11), else
  2. an inventory file `name address` per line, path in env `SRL_INVENTORY`, else
  3. the node name used verbatim (must be DNS/hosts-resolvable, or itself an IP).

Credentials default to admin / NokiaSrl1! and may be overridden per call or via env
`SRL_USER` / `SRL_PASSWORD`.

Host requirements: `gnmic`, `ssh`, and `sshpass` on PATH. gNMI on port 57400 (skip-verify),
SSH on port 22.
"""
import json, os, subprocess

DEFAULT_USER = os.environ.get("SRL_USER", "admin")
DEFAULT_PASSWORD = os.environ.get("SRL_PASSWORD", "NokiaSrl1!")
GNMI_PORT = int(os.environ.get("SRL_GNMI_PORT", "57400"))
SSH_PORT = int(os.environ.get("SRL_SSH_PORT", "22"))

_SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=10",
             "-o", "LogLevel=ERROR"]
# SR Linux CLI error markers — specific prefixes so the SSH login banner (which contains
# none of them) never trips a false "failure".
_ERR_MARKERS = ("Parsing error", "Error in path", "Failed to commit", "Error:",
                "Errors were", "is not valid", "Aborted")

_inventory = None

def _load_inventory():
    global _inventory
    if _inventory is not None:
        return _inventory
    _inventory = {}
    path = os.environ.get("SRL_INVENTORY")
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    _inventory[parts[0]] = parts[1]
    return _inventory

def node_addr(node):
    """Resolve a node name to its management host/IP (see module docstring)."""
    env = os.environ.get("SRL_ADDR_" + node)
    if env:
        return env
    inv = _load_inventory().get(node)
    if inv:
        return inv
    return node

def gnmi_get(node, paths, user=DEFAULT_USER, pw=DEFAULT_PASSWORD, host=None,
             datatype="state", timeout=60):
    """gNMI GET (json_ietf) via gnmic. Returns parsed JSON (list of results) or None."""
    host = host or node_addr(node)
    cmd = ["gnmic", "-a", f"{host}:{GNMI_PORT}", "-u", user, "-p", pw, "--skip-verify",
           "-e", "json_ietf", "get", "--type", datatype]
    for p in paths:
        cmd += ["--path", p]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

def sr_cli(node, lines, user=DEFAULT_USER, pw=DEFAULT_PASSWORD, host=None, timeout=120):
    """Run sr_cli commands on a node over SSH (fed on stdin — the reliable non-interactive
    path; the exec form drops into the interactive banner). `lines` is a list of CLI lines
    forming a complete session; a trailing `quit` is appended if missing. Returns
    (ok, combined_output). ok is False if SSH failed or the CLI emitted an error marker."""
    host = host or node_addr(node)
    lines = list(lines)
    if not lines or lines[-1].strip() != "quit":
        lines.append("quit")
    script = "\n".join(lines) + "\n"
    cmd = ["sshpass", "-p", pw, "ssh", *_SSH_OPTS, "-p", str(SSH_PORT), f"{user}@{host}"]
    try:
        r = subprocess.run(cmd, input=script, text=True, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        return False, f"missing SSH tooling ({e}); need `ssh` and `sshpass` on PATH"
    except Exception as e:
        return False, str(e)
    out = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0 and not any(m in out for m in _ERR_MARKERS)
    return ok, out

def commit(node, set_lines, **kwargs):
    """Apply `set`/`delete` CLI lines in a single candidate commit. Returns (ok, output)."""
    return sr_cli(node, ["enter candidate", *set_lines, "commit now", "quit"], **kwargs)
