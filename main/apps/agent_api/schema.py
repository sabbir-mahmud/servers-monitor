"""Validation for agent payloads. Hand-rolled (no DRF): the shapes are small and fixed, and
everything is bounded so one misbehaving agent can't bloat the servers table."""

import math

MAX_SERVICES = 100
MAX_DISKS = 32
MAX_ACTIONS = 100
MAX_LOGS = 100
SERVICE_STATES = {"up", "down", "unknown"}
FINAL_RESULT_STATES = {"running", "succeeded", "failed"}
MAX_OUTPUT_CHUNK = 64 * 1024


class PayloadError(ValueError):
    pass


def _str(value, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def _num(value, *, lo=None, hi=None, required=False, integer=False):
    if value is None:
        if required:
            raise PayloadError("missing required metric")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise PayloadError(f"not a number: {value!r}")
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return int(value) if integer else float(value)


def _pct(value, required=True):
    return _num(value, lo=0.0, hi=100.0, required=required)


def _list(value, limit):
    return [x for x in value[:limit] if isinstance(x, dict)] if isinstance(value, list) else []


def clean_inventory(inv) -> dict:
    """Containers, top processes, temperatures and GPUs (agent 2.2+). Bounded: a heartbeat
    must stay small whatever the agent sends."""
    if not isinstance(inv, dict) or not inv:
        return {}
    procs = inv.get("processes") if isinstance(inv.get("processes"), dict) else {}

    def proc(p):
        return {"pid": _num(p.get("pid"), lo=0, integer=True), "name": _str(p.get("name"), 60),
                "user": _str(p.get("user"), 32), "cpu": _num(p.get("cpu"), lo=0, hi=100_000),
                "rss": _num(p.get("rss"), lo=0, integer=True), "cmd": _str(p.get("cmd"), 120)}  # fmt: skip

    return {
        "containers": [
            {"id": _str(c.get("id"), 12), "name": _str(c.get("name"), 80), "image": _str(c.get("image"), 120),
             "state": _str(c.get("state"), 20), "cpu": _num(c.get("cpu"), lo=0, hi=100_000),
             "memory": _num(c.get("memory"), lo=0, integer=True)}
            for c in _list(inv.get("containers"), 50)
        ],
        "processes": {"cpu": [proc(p) for p in _list(procs.get("cpu"), 10)],
                      "memory": [proc(p) for p in _list(procs.get("memory"), 10)],
                      "count": _num(procs.get("count"), lo=0, integer=True)},
        "temps": [
            {"sensor": _str(t.get("sensor"), 60), "current": _num(t.get("current"), lo=-100, hi=300),
             "high": _num(t.get("high"), lo=0, hi=300), "critical": _num(t.get("critical"), lo=0, hi=300)}
            for t in _list(inv.get("temps"), 16) if t.get("sensor")
        ],
        "gpus": [
            {"name": _str(g.get("name"), 60), "util": _pct(g.get("util"), required=False),
             "mem_used": _num(g.get("mem_used"), lo=0), "mem_total": _num(g.get("mem_total"), lo=0),
             "temp": _num(g.get("temp"), lo=-100, hi=300)}
            for g in _list(inv.get("gpus"), 8)
        ],
    }  # fmt: skip


def clean_report(data) -> dict:
    if not isinstance(data, dict):
        raise PayloadError("body must be a JSON object")
    host = data.get("host") or {}
    metrics = data.get("metrics") or {}
    caps = data.get("capabilities") or {}
    if not all(isinstance(x, dict) for x in (host, metrics, caps)):
        raise PayloadError("host, metrics and capabilities must be objects")

    services = []
    for svc in (data.get("services") or [])[:MAX_SERVICES]:
        if not isinstance(svc, dict) or not svc.get("name"):
            continue
        state = svc.get("state") if svc.get("state") in SERVICE_STATES else "unknown"
        services.append(
            {
                "name": _str(svc["name"], 100),
                "kind": _str(svc.get("kind"), 20),
                "state": state,
                "status": _str(svc.get("status"), 60),
                "version": _str(svc.get("version"), 100),
                "restartable": bool(svc.get("restartable")),
            }
        )

    disks = []
    for d in (data.get("disks") or [])[:MAX_DISKS]:
        if isinstance(d, dict) and d.get("mount"):
            disks.append(
                {
                    "mount": _str(d["mount"], 200),
                    "total": _num(d.get("total"), lo=0, integer=True),
                    "used": _num(d.get("used"), lo=0, integer=True),
                    "percent": _pct(d.get("percent"), required=False),
                }
            )

    actions = []
    for a in (caps.get("actions") or [])[:MAX_ACTIONS]:
        if isinstance(a, dict) and a.get("name"):
            actions.append(
                {
                    "name": _str(a["name"], 64),
                    "description": _str(a.get("description"), 200),
                    "timeout": _num(a.get("timeout"), lo=1, hi=86400, integer=True) or 300,
                }
            )
    logs = [_str(n, 100) for n in (caps.get("logs") or [])[:MAX_LOGS] if n]

    return {
        "hostname": _str(host.get("hostname"), 255),
        "platform": _str(host.get("platform"), 255),
        "agent_version": _str(data.get("agent_version"), 32),
        "cpu_count": _num(host.get("cpu_count"), lo=0, hi=65535, integer=True),
        "memory_total": _num(host.get("memory_total"), lo=0, integer=True),
        "uptime_seconds": _num(host.get("uptime_seconds"), lo=0, integer=True),
        "cpu_percent": _pct(metrics.get("cpu_percent")),
        "memory_percent": _pct(metrics.get("memory_percent")),
        "disk_percent": _pct(metrics.get("disk_percent")),
        "load_1": _num(metrics.get("load_1"), lo=0),
        "net_rx_bps": _num(metrics.get("net_rx_bps"), lo=0),
        "net_tx_bps": _num(metrics.get("net_tx_bps"), lo=0),
        "services": services,
        "disks": disks,
        "capabilities": {
            "actions": actions,
            "logs": logs,
            "shell": bool(caps.get("shell")),
            "features": [
                _str(f, 32) for f in (caps.get("features") or [])[:32] if isinstance(f, str)
            ],
        },
        "inventory": clean_inventory(data.get("inventory")),
    }


def clean_result(data) -> dict:
    if not isinstance(data, dict):
        raise PayloadError("body must be a JSON object")
    status = data.get("status")
    if status not in FINAL_RESULT_STATES:
        raise PayloadError(f"status must be one of {sorted(FINAL_RESULT_STATES)}")
    output = data.get("output") or ""
    if not isinstance(output, str):
        raise PayloadError("output must be a string")
    exit_code = data.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise PayloadError("exit_code must be an integer")
    return {"status": status, "output": output[:MAX_OUTPUT_CHUNK], "exit_code": exit_code}
