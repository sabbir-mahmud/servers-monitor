# Server Monitor: competitive analysis, gaps and plan

*October 2026. Competitor facts come from public documentation and third-party reviews
(see [Sources](#sources)); where those disagree, it's noted. Scores are judgments, with the
reasoning shown so you can re-weight them.*

---

## 1. Summary

**Server Monitor occupies a gap the other tools leave:** monitoring **and** operating a
small fleet (deploys, restarts, logs) from a dashboard that needs **no inbound access anywhere**,
with a security model built for that (signed commands, per-server allowlists, verified
installs).

| | |
|---|---|
| **Where it leads** | Security model, zero-exposure architecture, monitoring + operations in one lightweight tool |
| **Where it trails** | Metric depth (no containers, processes, temperatures), integrations (no API, SSO or many alert channels), scale (SQLite, ~hundreds of servers) and status pages |
| **Biggest risk** | *Who watches the watcher?* If the dashboard machine sleeps or dies, nobody is alerted. Every SaaS competitor avoids this by design. |
| **Ranking** | **2nd of 12** when weighted for its target user (behind Datadog, at a fraction of the cost), but **7th of 12** when weighted for pure monitoring. That second number is the honest one to improve. |

**Top 5 next moves:** (1) a watchdog for the dashboard itself, (2) built-in backups, (3)
two-factor login, (4) public status pages, (5) disk-full forecasts and cron heartbeat monitors.

---

## 2. The landscape

The alternatives fall into five groups. Nobody else combines all of these columns.

| Group | Tools | What they do well | What they don't do |
|---|---|---|---|
| **Server metrics (self-hosted)** | Netdata, Beszel, Zabbix, Prometheus + Grafana, Checkmk | Deep, high-resolution metrics, mature alerting | Operations (deploy, restart, run); most need inbound access or a heavy stack |
| **Uptime (self-hosted / SaaS)** | Uptime Kuma, UptimeRobot, Better Stack, Healthchecks.io | HTTP/TCP/ping checks, status pages, many notification channels | Server internals, operations |
| **Fleet ops & deploy** | Komodo, Coolify, Dokploy, Portainer | Docker builds, Git-driven deploys, containers | Real monitoring and alerting depth; usually root access from the controller |
| **Job runners** | Semaphore UI, Rundeck, AWX | Ansible/scripts across hosts, schedules, RBAC | Monitoring; need SSH into every host (inbound) |
| **Enterprise SaaS** | Datadog, New Relic | Everything, at scale | Cost (Datadog ≈ $15–23 per host per month), data leaves your control |

---

## 3. Feature comparison

✅ built in · ⚠️ partial or add-on · ❌ not available

| Capability | **Server Monitor** | Beszel | Netdata | Uptime Kuma | Zabbix | Prom+Grafana | Komodo | Coolify / Dokploy | Semaphore / Rundeck | Better Stack | Datadog |
|---|---|---|---|---|---|---|---|---|---|---|---|
| No inbound ports on servers | ✅ | ✅¹ | ✅ | n/a | ⚠️ | ❌ | ✅¹ | ❌ | ❌ (SSH) | n/a | ✅ |
| Dashboard needs no public exposure | ✅ | ❌ | ⚠️ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | SaaS | SaaS |
| CPU / memory / disk / network | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ⚠️ | ✅ | ❌ | ❌ | ✅ |
| Containers / processes / temps / GPU | ❌ | ✅ | ✅ | ❌ | ✅ | ✅ | ⚠️ | ⚠️ | ❌ | ❌ | ✅ |
| Services & versions (systemd, Docker, pm2) | ✅ | ⚠️² | ⚠️ | ❌ | ✅ | ⚠️ | ⚠️ | ⚠️ | ❌ | ❌ | ✅ |
| HTTP checks, keyword, TLS expiry | ✅ | ❌ | ⚠️ | ✅ | ✅ | ⚠️ | ❌ | ❌ | ❌ | ✅ | ✅ |
| TCP / ping / DNS / domain expiry | ❌ | ❌ | ⚠️ | ✅ | ✅ | ⚠️ | ❌ | ❌ | ❌ | ✅ | ✅ |
| Cron / heartbeat monitors | ❌ | ❌ | ❌ | ✅ | ⚠️ | ⚠️ | ❌ | ❌ | ❌ | ✅ | ✅ |
| Public status page | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ⚠️ |
| Run actions / deploy across servers | ✅ | ❌ | ❌ | ❌ | ⚠️ | ❌ | ✅ | ✅ | ✅ | ❌ | ⚠️ |
| Per-server allowlist of what may run | ✅ | n/a | n/a | n/a | ⚠️ | n/a | ❌ | ❌ | ❌ | n/a | ❌ |
| Signed commands / verified agent installs | ✅ | ❌ | ❌ | n/a | ❌ | n/a | ❌ | ❌ | ❌ | n/a | ⚠️ |
| Scheduled / rolling jobs | ❌ | n/a | n/a | n/a | ⚠️ | n/a | ✅ | ⚠️ | ✅ | n/a | ⚠️ |
| Telegram + webhooks, routing, reminders | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | ⚠️ | ✅ | ✅ |
| Email / SMS / PagerDuty / on-call | ❌ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ⚠️ | ✅ | ✅ |
| Maintenance windows | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ |
| Roles, invites, audit log | ✅ | ⚠️ | ✅ | ⚠️ | ✅ | ⚠️ | ✅ | ⚠️ | ✅ | ✅ | ✅ |
| 2FA / SSO (OIDC) | ❌ | ✅³ | ✅ | ⚠️ | ✅ | ⚠️ | ✅ | ⚠️ | ✅ | ✅ | ✅ |
| REST API | ❌ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Long retention / scale beyond hundreds of servers | ❌ | ⚠️ | ✅ | ⚠️ | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | ✅ | ✅ |
| Footprint (dashboard RAM) | ~200 MB | small | ~150 MB per node⁴ | small | large | large | medium (+MongoDB) | 350–800 MB⁵ | 50 MB–2 GB⁶ | SaaS | SaaS |
| Price (self-hosted) | free + Redis (~$0–6/mo) | free | free agent; cloud from ~$4.50/node⁷ | free | free | free | free | free | free | free tier; paid from ~$29/mo⁸ | ~$15–23/host/mo |

¹ Agents can open the connection to the hub/Core (Komodo since v2). ² Beszel reportedly
added failed-systemd-service alerts in 0.19 (unverified). ³ OAuth/OIDC login. ⁴ Netdata's
own resource guidance. ⁵ Third-party measurements disagree. ⁶ Semaphore is light, Rundeck
heavy (JVM). ⁷ Business plan, annual; free-tier node limits reported inconsistently. ⁸ Sources
disagree on Better Stack's paid entry price.

---

## 4. Scored ranking

### Criteria (1–5 each)

| Criterion | What earns a 5 |
|---|---|
| **A. Server metrics** | Deep, high-resolution metrics with history: containers, processes, hardware |
| **B. Uptime / app monitoring** | Many check types, status pages, heartbeats |
| **C. Alerting** | Many channels, routing, dedup, maintenance, escalation |
| **D. Operations** | Run, deploy and schedule work across servers safely |
| **E. Security model** | No exposure, least privilege, integrity of commands and code |
| **F. Setup & upkeep** | Minutes to run, little to maintain |
| **G. Footprint** | Light on the host |
| **H. Scale** | Thousands of servers and long retention |
| **I. Ecosystem** | API, SSO, integrations, community |
| **J. Cost** | Free or near-free at small scale |

### Scores

| Tool | A | B | C | D | E | F | G | H | I | J |
|---|---|---|---|---|---|---|---|---|---|---|
| **Server Monitor** | 3 | 4 | 4 | 4 | 5 | 4 | 5 | 2 | 2 | 4 |
| Datadog | 5 | 5 | 5 | 2 | 4 | 5 | 3 | 5 | 5 | 1 |
| Zabbix | 5 | 4 | 5 | 2 | 3 | 1 | 2 | 5 | 5 | 5 |
| Netdata | 5 | 2 | 4 | 1 | 3 | 4 | 3 | 5 | 5 | 4 |
| Better Stack / UptimeRobot | 1 | 5 | 5 | 1 | 4 | 5 | 5 | 4 | 5 | 3 |
| Prometheus + Grafana | 5 | 4 | 4 | 1 | 3 | 1 | 2 | 5 | 5 | 5 |
| Uptime Kuma | 1 | 5 | 5 | 1 | 3 | 5 | 4 | 3 | 4 | 5 |
| Coolify / Dokploy | 2 | 2 | 3 | 5 | 2 | 4 | 2 | 3 | 4 | 5 |
| Healthchecks.io | 1 | 2 | 4 | 1 | 4 | 5 | 5 | 4 | 4 | 5 |
| Beszel | 4 | 1 | 3 | 1 | 3 | 5 | 5 | 3 | 3 | 5 |
| Komodo | 2 | 1 | 2 | 5 | 3 | 3 | 3 | 4 | 3 | 5 |
| Semaphore / Rundeck | 1 | 1 | 3 | 5 | 3 | 3 | 3 | 4 | 4 | 5 |

**Why Server Monitor gets these scores.** A = 3: core host metrics and 14 days of history, but
no containers, processes or temperatures. H = 2: SQLite and a per-heartbeat Redis cost cap it
at hundreds of servers. I = 2: no API, no SSO, only two notification channel types. E = 5:
the only tool here with signed commands, per-server Redis isolation, verified installs and
zero exposure on both ends.

### Ranking A: the target user

*A small team operating 1–50 servers that wants monitoring **and** operations without exposing
anything.* Weights: A 15 · B 10 · C 10 · **D 15** · **E 15** · F 10 · G 5 · H 10 · I 5 · J 5.

| Rank | Tool | Score / 100 |
|---|---|---|
| 1 | Datadog | **82** |
| **2** | **Server Monitor** | **75** |
| 3 | Zabbix | 72 |
| 4 | Netdata | 69 |
| 4 | Better Stack / UptimeRobot | 69 |
| 6 | Prometheus + Grafana | 67 |
| 7 | Uptime Kuma | 64 |
| 8 | Coolify / Dokploy | 62 |
| 8 | Healthchecks.io | 62 |
| 10 | Beszel | 61 |
| 10 | Komodo | 61 |
| 10 | Semaphore / Rundeck | 61 |

Datadog wins on breadth but costs roughly $15–23 per host per month, sends data off-site and
can't run your deploy scripts under a local allowlist. Server Monitor is the leading
**self-hosted** option for this user.

### Ranking B: pure monitoring

*Someone who only wants to watch servers and sites.* Weights: **A 25 · B 15 · C 20** · D 0 ·
E 5 · F 10 · G 5 · **H 15** · I 5 · J 0.

| Rank | Tool | Score / 100 |
|---|---|---|
| 1 | Datadog | 97 |
| 2 | Zabbix | 84 |
| 3 | Netdata | 81 |
| 4 | Prometheus + Grafana | 80 |
| 5 | Better Stack / UptimeRobot | 76 |
| 6 | Uptime Kuma | 70 |
| 7 | **Server Monitor** | **69** |
| 8 | Beszel | 65 |

**The takeaway:** the project's differentiation is operations plus security. Its weak spots
(metric depth, check types, status pages, integrations) are exactly what pure-monitoring
users compare on. Closing a handful of them (section 5) would move it to about 4th in this
ranking without losing its niche.

---

## 5. Missing features, ranked

Priority = **Reach × Impact ÷ Effort**:
- **Reach**: share of users who'd use it, 1–5
- **Impact**: how much it matters to them, 1–5
- **Effort**: S = 1, M = 2, L = 3, XL = 4

| # | Feature | Why it matters | Competitors with it | Reach | Impact | Effort | **Priority** |
|---|---|---|---|---|---|---|---|
| 1 | **Watchdog for the dashboard itself** | If the dashboard machine sleeps or dies, nobody is alerted. The relay pings an external heartbeat (Healthchecks.io free tier, or a Telegram alert when it comes back after a gap). | All SaaS by design | 5 | 5 | S | **25** |
| 2 | **Built-in backups** (Litestream to S3/R2 + `.env` reminder) | All history and config is one SQLite file on one machine. | Coolify (S3 backups), SaaS | 3 | 4 | S | **12** |
| 3 | **Two-factor login (TOTP + recovery codes)** | This dashboard can deploy to every server; a password alone is thin. | Netdata, Zabbix, Better Stack, Datadog | 4 | 5 | M | **10** |
| 4 | **Public status pages** | Customers see uptime without your dashboard being exposed; push a static page to GitHub/Cloudflare Pages. | Uptime Kuma, Better Stack, UptimeRobot | 5 | 4 | M | **10** |
| 5 | **Disk-full forecast** ("/var fills in ~6 days") | Turns the most common outage into a planned task; cheap linear fit on existing samples. | Netdata (ML), Datadog (forecasts) | 3 | 3 | S | **9** |
| 6 | **Cron / heartbeat monitors** (`smctl ping backup` through the relay, or a push URL) | Catches silent backup and cron failures that uptime checks never see. | Healthchecks.io, Better Stack, Uptime Kuma | 4 | 4 | M | **8** |
| 7 | **More check types**: TCP port, ping, DNS record, domain expiry, keyword *absent*, custom method/headers/body | Databases, mail servers and APIs aren't plain GET pages. | Uptime Kuma, Zabbix, Better Stack | 4 | 3 | M | **6** |
| 8 | **More channels**: email, ntfy, Gotify, Pushover, Discord/Slack native, PagerDuty/Opsgenie | People live in different apps; teams need paging. | Uptime Kuma (90+), Beszel, Zabbix | 4 | 3 | M | **6** |
| 9 | **Agent upgrades from the dashboard** (verified, per server or fleet-wide) | No SSH-ing in to run `smctl upgrade` on every box. | Netdata, Komodo, Datadog | 4 | 3 | M | **6** |
| 10 | **Redis usage meter** (monthly commands + cost estimate) | Avoids surprise Upstash bills or free-tier cut-offs. | n/a (unique to this architecture) | 3 | 2 | S | **6** |
| 11 | **Containers, top processes, temperatures, GPU** | The most-requested metric depth; Docker hosts are the norm. | Beszel, Netdata, Zabbix, Datadog | 4 | 4 | L | **5.3** |
| 12 | **Scheduled and rolling jobs** (cron in the dashboard; canary, then batches, stop on failure) | Turns "run on servers" into safe, repeatable deploys. | Komodo, Semaphore, Rundeck | 3 | 5 | L | **5** |
| 13 | **Multi-location checks using agents as probes** | Removes single-vantage false alarms with no extra infrastructure. | Better Stack, UptimeRobot (paid) | 3 | 4 | L | **4** |
| 14 | **REST API + API tokens**, Prometheus `/metrics` export | Automation, Terraform, Grafana for those who want it. | Everyone except Uptime Kuma | 3 | 3 | L | **3** |
| 15 | **Downsampled long-term history** (1-min → 1-hour rollups, 1 year) | Capacity planning and trend questions. | Netdata, Zabbix, Prometheus | 3 | 3 | L | **3** |
| 16 | **Live log follow** (stream, journald, search last N lines) | Debugging without SSH. | Komodo, Coolify, Datadog | 3 | 3 | L | **3** |
| 17 | **Mobile-friendly PWA + push notifications** | Glance and acknowledge from a phone. | Better Stack, Datadog apps | 3 | 2 | M | **3** |
| 18 | **SSO (OIDC)** | Teams with Google Workspace or Entra ID. | Beszel, Komodo, Zabbix | 2 | 3 | L | **2** |
| 19 | Projects / multi-tenancy | Agencies managing several clients. | Zabbix, Datadog | 2 | 3 | XL | 1.5 |
| 20 | Windows / macOS agents | Mixed fleets. | Zabbix, Netdata, Datadog | 2 | 3 | XL | 1.5 |
| 21 | On-call schedules and incident postmortems | Larger teams. | Better Stack, PagerDuty | 2 | 3 | XL | 1.5 |
| 22 | SNMP / network devices | Routers, switches, UPS. | Zabbix, Checkmk | 2 | 3 | XL | 1.5 |

---

## 6. Roadmap

### Phase 1: trust the system (≈ 2 weeks)
1. **Dashboard watchdog** (#1): the relay pings `WATCHDOG_URL` every minute, plus a
   "dashboard was down from X to Y" message on recovery.
2. **Backups** (#2): Litestream sidecar in `docker-compose.yml` and a backup-status tile.
3. **Two-factor login** (#3): TOTP with recovery codes, required for Admins (configurable).
4. **Redis usage meter** (#10) and **disk-full forecast** (#5).

*Outcome:* the "who watches the watcher" risk is gone, and data and accounts are protected.

### Phase 2: catch what's missed (≈ 3–4 weeks)
5. **Heartbeat monitors** (#6): `smctl ping NAME` through the relay, so still no public endpoint.
6. **Check types** (#7): TCP, ping, DNS, domain expiry, keyword absence, custom requests.
7. **Channels** (#8): email (reusing `MAILERS`), ntfy, Gotify, Pushover, PagerDuty.
8. **Status pages** (#4): static pages published to GitHub Pages or Cloudflare Pages.

*Outcome:* about 4th in the pure-monitoring ranking, the strongest open-source "monitor +
operate" option.

### Phase 3: operate at scale (≈ 4–6 weeks)
9. **Containers, top processes, temperatures** (#11).
10. **Scheduled and rolling jobs** (#12).
11. **Verified agent upgrades from the dashboard** (#9).
12. **Agents as uptime probes** (#13).

### Phase 4: platform (as demand shows)
REST API and `/metrics` (#14), rollups (#15), live logs (#16), PWA (#17), SSO (#18).

### Deliberately not planned
Multi-tenancy, Windows agents, on-call scheduling and SNMP (#19–22). Each is large, serves
few of the target users, and pulls the project toward Zabbix/Datadog territory, where it
can't win. Revisit only with clear demand.

---

## 7. Risks to watch

| Risk | Mitigation |
|---|---|
| Dashboard availability = alerting availability | Phase 1 watchdog; recommend an always-on host (Pi or small VPS) |
| Redis provider dependency and cost at scale | Usage meter; document self-hosted Valkey; adaptive heartbeat intervals |
| SQLite limits past a few hundred servers | Rollups (#15); optional PostgreSQL once needed |
| Single maintainer / bus factor | CONTRIBUTING.md, tests (163), architecture docs, labelled "good first issue"s |
| Security claims are the brand | Keep `test_security.py` growing; consider an external review before 1.0 |

---

## Sources

- Beszel: [Linux Magazine (2026)](https://www.linux-magazine.com/index.php/Issues/2026/310/Beszel), [dev.co](https://dev.co/devops/open-source/beszel), [0.19 guide](https://aicybr.com/blog/beszel-0-19-self-hosting-monitoring-guide), [pkg.go.dev](https://pkg.go.dev/github.com/henrygd/beszel@v0.18.7)
- Uptime Kuma: [AlternativeTo: 2.0 release](https://alternativeto.net/news/2025/10/uptime-kuma-2-0-introduces-mariadb-support-rootless-docker-modern-ui-and-upgrade-tools), [Releasebot changelog](https://releasebot.io/updates/louislam/uptime-kuma)
- Netdata: [MonitoringCost](https://monitoringcost.com/netdata-pricing), [CubeAPM review](https://cubeapm.com/blog/netdata-pricing-review/), [Toolradar](https://toolradar.com/tools/netdata/pricing), [Capterra](https://www.capterra.com/p/251845/Netdata/pricing/)
- Komodo: [Botmonster: Komodo vs Portainer vs Dockge 2026](https://botmonster.com/self-hosting/komodo-vs-portainer-vs-dockge-2026-homelab-decision-guide/), [VirtualizationHowTo](https://www.virtualizationhowto.com/2024/12/portainer-alternative-komodo-for-docker-stack-management-and-deployment/), [Jacar](https://jacar.es/en/how-to-install-komodo-with-docker/), [Bitdoze](https://www.bitdoze.com/portainer-alternatives/)
- Zabbix / Prometheus / Checkmk: [WZ-IT comparison](https://wz-it.com/en/blog/zabbix-checkmk-prometheus-netdata-monitoring-comparison/), [SigNoz: Zabbix alternatives](https://signoz.io/comparisons/zabbix-alternatives/), [The CTO Club](https://thectoclub.com/tools/zabbix-vs-prometheus/)
- Coolify / Dokploy: [Cherry Servers](https://www.cherryservers.com/blog/coolify-vs-dokploy), [INTROSERV 2026](https://introserv.com/blog/dokploy-vs-coolify-complete-comparison-of-the-best-self-hosted-paas-platforms-for-vps-and-dedicated-servers-2026/), [getdeploying](https://getdeploying.com/guides/coolify-vs-dokploy)
- Semaphore / Rundeck: [Semaphore features](https://semaphoreui.com/es/features), [pistack 2026 guide](https://www.pistack.xyz/posts/semaphore-vs-awx-vs-rundeck-self-hosted-ansible-ui-guide-2026/)
- Healthchecks.io: [Capterra](https://www.capterra.com.au/software/1028488/healthchecksio), [GetApp](https://www.getapp.ie/software/2063944/healthchecks-io)
- Better Stack / UptimeRobot: [costbench: Better Stack free plan](https://costbench.com/software/log-management/better-stack/free-plan/), [notifier.so: Better Stack vs UptimeRobot](https://notifier.so/guides/better-stack-vs-uptimerobot/), [notifier.so: UptimeRobot pricing 2026](https://notifier.so/guides/uptimerobot-pricing-2026/), [velprove](https://velprove.com/blog/uptimerobot-commercial-alternative)
- Datadog: [SigNoz: Datadog pricing](https://signoz.io/blog/datadog-pricing/), [Last9](https://last9.io/blog/datadog-pricing-all-your-questions-answered.md), [OneUptime: real cost of Datadog](https://oneuptime.com/blog/post/2026-03-18-the-real-cost-of-datadog/markdown)
