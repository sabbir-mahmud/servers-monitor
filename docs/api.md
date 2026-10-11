# REST API and Prometheus metrics

A small JSON API under `/api/v1/` and a Prometheus endpoint at `/metrics`, for scripts, CI
pipelines, Grafana and other tools that can **already reach your dashboard**. Adding the API
doesn't make the dashboard public: keep it on a private network, a VPN or behind your proxy's
access rules, as before.

- [Tokens](#tokens)
- [Conventions](#conventions)
- [Endpoints](#endpoints)
- [Running actions](#running-actions)
- [Prometheus](#prometheus)
- [OpenAPI summary](#openapi-summary)

## Tokens

Create one under **My account → API tokens**. The token (`smt_…`) is shown **once**; only a
SHA-256 hash is stored.

| Scope | What it can do |
|---|---|
| **Read only** | Every `GET` endpoint and `/metrics` |
| **Read and run actions** | Also `POST /api/v1/jobs/` (offered to Operators and Admins) |

A token acts **as you**: it can never do more than your role allows, and if your account is
deactivated every token you own stops working. Tokens can expire (30 days, 90 days, a year)
and can be revoked at any time. Creating and revoking tokens, and every job started with
one, is in the audit log (`via: API token <name>`).

Send it as a bearer token:

```bash
curl -H "Authorization: Bearer smt_…" https://monitor.internal/api/v1/servers/
```

The API never looks at the browser session, so a signed-in browser can't be tricked into
calling it (no CSRF exposure).

## Conventions

- JSON in and out; times are ISO 8601 in UTC; ids are UUIDs.
- Lists take `?limit=` (default 100, max 500) and `?offset=`, and return
  `{"results": [...], "next_offset": 200 | null}`.
- Errors return `{"error": "message"}` with `400` (bad input), `401` (missing, invalid,
  revoked or expired token), `403` (your role or the token's scope doesn't allow it),
  `404`, `405` or `429`.
- **Rate limit:** `API_RATE_LIMIT` requests a minute per token (default 120). Over it you
  get `429` with `Retry-After: 60`. Repeated bad tokens from one IP are throttled too.

## Endpoints

| Method | Path | Needs (role) | Returns |
|---|---|---|---|
| GET | `/api/v1/servers/` | Viewer | Servers. Filters: `environment`, `group` |
| GET | `/api/v1/servers/{id}/` | Viewer | One server with services, disks, actions, logs, features |
| GET | `/api/v1/monitors/` | Viewer | Application monitors with 24 h uptime. Filter: `status` (`up`, `degraded`, `down`, `pending`) |
| GET | `/api/v1/monitors/{id}/` | Viewer | One monitor |
| GET | `/api/v1/alerts/` | Viewer | Alerts. `state=open` (default), `resolved` or `all` |
| GET | `/api/v1/jobs/` | Viewer | Recent jobs with per-status counts |
| GET | `/api/v1/jobs/{id}/` | Viewer | One job with its commands |
| POST | `/api/v1/jobs/` | Operator, token with *run actions* | Start a job (below) |
| GET | `/api/v1/commands/{id}/` | Viewer | One command with its output |
| GET | `/metrics` | Viewer | Prometheus text format |

A server looks like this:

```json
{
  "id": "0192f0c4-…", "name": "web-01", "environment": "production", "groups": ["api"],
  "enabled": true, "health": "online", "last_seen_at": "2026-10-11T09:30:02+00:00",
  "agent_version": "2.2.0", "hostname": "web-01",
  "metrics": {"cpu_percent": 12.5, "memory_percent": 40.0, "disk_percent": 50.0,
              "load_1": 0.4, "net_rx_bps": 1000.0, "net_tx_bps": 500.0},
  "disk_full_at": null
}
```

## Running actions

```bash
curl -X POST https://monitor.internal/api/v1/jobs/ \
  -H "Authorization: Bearer smt_…" -H "Content-Type: application/json" \
  -d '{"action": "deploy_api", "servers": ["web-01", "web-02"], "note": "CI build 512",
       "rollout": {"batch_size": 1, "pause_seconds": 60, "stop_on_failure": true}}'
```

| Field | |
|---|---|
| `action` | An action the servers' agents declare, or `tail_log` / `restart_service` |
| `args` | For `tail_log`: `{"name": "nginx_error", "lines": 200}`; for `restart_service`: `{"name": "nginx"}` |
| `servers` | Server ids or names |
| `note` | Optional, shown on the job |
| `rollout` | Optional: roll out in batches (see [Scheduled and rolling jobs](agent.md#scheduled-and-rolling-jobs)) |

The response is `201` with the job (as `GET /api/v1/jobs/{id}/`) plus `skipped`: servers that
can't run the action and why. Poll the job, or `GET /api/v1/commands/{id}/` for output.

The same checks apply as in the dashboard: the dashboard only sends actions a server
advertises, and each agent refuses anything its own allowlist doesn't declare. **Free-form
shell is browser-only**, whatever the token or role.

## Prometheus

```yaml
scrape_configs:
  - job_name: server-monitor
    scheme: https
    metrics_path: /metrics
    authorization:
      credentials: smt_…        # a read-only token
    static_configs:
      - targets: ["monitor.internal"]
```

| Metric | Labels | |
|---|---|---|
| `sm_build_info` | `version` | Always 1 |
| `sm_server_up` | `server`, `environment` | 1 if the agent reported recently |
| `sm_server_last_seen_timestamp_seconds` | `server`, `environment` | |
| `sm_server_cpu_percent`, `_memory_percent`, `_disk_percent`, `_load_1`, `_net_rx_bps`, `_net_tx_bps` | `server`, `environment` | Latest heartbeat |
| `sm_server_disk_used_percent` | `server`, `environment`, `mount` | Per filesystem |
| `sm_service_up` | `server`, `environment`, `service`, `kind` | 1 if up |
| `sm_monitor_up` | `monitor`, `kind` | 1 if up or slow, 0 if down |
| `sm_monitor_response_ms` | `monitor`, `kind` | Last response time |
| `sm_monitor_tls_expiry_timestamp_seconds` | `monitor`, `kind` | |
| `sm_alerts_open` | `severity` | Open alerts |

Values are the latest snapshot: scrape every 1–2 minutes (agents report every 2 minutes by
default). Disabled servers and paused monitors are left out.

## OpenAPI summary

```yaml
openapi: 3.1.0
info: {title: Server Monitor API, version: "1"}
servers: [{url: /api/v1}]
components:
  securitySchemes:
    token: {type: http, scheme: bearer}
  schemas:
    Error: {type: object, properties: {error: {type: string}}}
    Page:
      type: object
      properties:
        results: {type: array, items: {type: object}}
        next_offset: {type: [integer, "null"]}
security: [{token: []}]
paths:
  /servers/:
    get:
      parameters:
        - {name: environment, in: query, schema: {type: string}}
        - {name: group, in: query, schema: {type: string}}
        - {name: limit, in: query, schema: {type: integer, maximum: 500}}
        - {name: offset, in: query, schema: {type: integer}}
      responses: {"200": {description: Servers}, "401": {description: Bad token}}
  /servers/{id}/:
    get: {responses: {"200": {description: Server}, "404": {description: Not found}}}
  /monitors/:
    get:
      parameters: [{name: status, in: query, schema: {enum: [up, degraded, down, pending]}}]
      responses: {"200": {description: Monitors}}
  /monitors/{id}/:
    get: {responses: {"200": {description: Monitor}}}
  /alerts/:
    get:
      parameters: [{name: state, in: query, schema: {enum: [open, resolved, all]}}]
      responses: {"200": {description: Alerts}}
  /jobs/:
    get: {responses: {"200": {description: Jobs}}}
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [action, servers]
              properties:
                action: {type: string}
                args: {type: object}
                servers: {type: array, items: {type: string}}
                note: {type: string, maxLength: 200}
                rollout:
                  type: object
                  properties:
                    batch_size: {type: integer, minimum: 1}
                    pause_seconds: {type: integer, minimum: 0}
                    stop_on_failure: {type: boolean}
      responses:
        "201": {description: Job created}
        "400": {description: Invalid request or no server can run it}
        "403": {description: Read-only token, role, or a browser-only action}
  /jobs/{id}/:
    get: {responses: {"200": {description: Job with commands}}}
  /commands/{id}/:
    get: {responses: {"200": {description: Command with output}}}
```
