# os-fleet-agent — OPNsense Push-Mode Agent

OPNsense plugin that ships local status to an [os-fleet](https://github.com/KaiOppi/os-fleet) server via **outbound HTTPS only** — ideal for boxes behind NAT, on dynamic IPs, or behind strict customer-side firewalls.

The agent periodically collects:

- system identity + firmware status (`/api/diagnostics/system/system_information`, `/api/core/firmware/{status,info}`)
- installed plugins, including those dropped into `config.xml` without firmware metadata
- certificates (`/api/trust/cert/search`)
- the full `config.xml` (parsed server-side for rules + aliases, never persisted verbatim)
- hardware health (RAM/disk/load/uptime/temp)

…and POSTs it to the configured os-fleet server. It also runs a **quick-poll command loop** for sub-minute remote operations (firmware checks, plugin install, legacy XML rule edits, NAT lifecycle, …) — all dispatched server-side, executed locally with the box's own admin key.

> **Status:** alpha — works in production on the author's fleet, but the os-fleet server side is still pre-1.0. Use against a dedicated tenant and stay close to the changelog.

## Installation

In the OPNsense shell (Console → option 8):

```sh
pkg add https://github.com/KaiOppi/os-fleet-agent/releases/download/v0.0.11/os-fleet-agent-0.0.11.pkg
```

Then in the WebGUI under **Services → Fleet Agent**:

1. Set **Server URL** (your os-fleet base URL, e.g. `https://os-fleet.example.com`)
2. Paste the **Agent Token** from os-fleet's box-add modal (push-mode toggle)
3. Set **Local API key/secret** so the agent can call `/api/...` on this box
4. Hit **Test Connection** — should show `whoami: ok`
5. Save — the cron job is registered automatically (default: every 60 s)

## What it sends

Outbound only, never inbound. The agent never opens a listener.

| Endpoint | Direction | Frequency |
|---|---|---|
| `POST /api/fleet/ingest/<token>` | agent → server | every cron tick (default 60 s) |
| `POST /api/fleet/ingest/<token>/quick` | agent → server | quick-poll, 19× per cycle |

The Agent-Token is rotatable from the os-fleet UI; old tokens stop working immediately.

## What it can do remotely

Server-side commands are queued and consumed by the quick-poll loop. Capabilities advertised:

- `firmware_check_sync` — fresh `/firmware/status` in the cmd response (latency <5 s)
- `plugin_install_pkg` — `pkg add -f <url>` against allowlisted hosts (`github.com`, `*.githubusercontent.com`)
- `legacy_filter_update` — edit `<filter><rule>` in `config.xml` by stable hash, then `configctl filter reload` with rollback on syntax-fail
- `legacy_nat_{create,update,toggle,delete}` — same for `<nat><rule>` (port-forward)
- `legacy_outbound_{create,update,toggle,delete}` — same for `<nat><outbound><rule>`
- `force_status_push` — manual trigger; bypasses the cron interval
- `api_call` — generic Pull-on-Push to the local OPNsense API (anything the configured admin key can do)

All file mutations follow the same pattern: backup → atomic write → `xmllint`-style sanity-parse → reload → rollback on failure.

## Development

```sh
# Sync to the dev OPNsense (additive rsync, never deletes; use deploy-dev.sh)
DEV_HOST=opnsense-dev DEV_PASS='…' ./deploy-dev.sh

# Build a .pkg ON the dev sense for release upload:
ssh opnsense-dev
mkdir -p /root/os-fleet-agent-build
rsync -a /path/to/this-checkout/ /root/os-fleet-agent-build/
cd /root/os-fleet-agent-build && ./build-pkg.sh
# → /root/os-fleet-agent-<version>.pkg
```

## License

[BSD 2-Clause](LICENSE) — same as the OPNsense plugin ecosystem.

## Companion project

This plugin is part of [**os-fleet**](https://github.com/KaiOppi/os-fleet) (multi-OPNsense fleet management). It works only with a running os-fleet server.
