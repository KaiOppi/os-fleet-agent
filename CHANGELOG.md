# Changelog

All notable changes to **os-fleet-agent** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.11] — 2026-05-06

First release as a standalone repository — code previously lived at `KaiOppi/os-fleet/plugins/os-fleet-agent/` and is now split out so it can be registered as a Plugin Source in the os-fleet server.

### Added (cumulative since the agent first appeared in `os-fleet` v0.2.0)
- Outbound-only push to `/api/fleet/ingest/<token>` with system info, firmware status, plugins, certs, and `config.xml`.
- Quick-poll loop (`/ingest/<token>/quick`): 19 iterations × 3 s sleep — sub-minute cmd-dispatch latency.
- Pull-on-Push command bus: `ping`, `get_log`, `api_call`.
- `firmware_check_sync` capability — fresh `/firmware/status` returned inline with the cmd result.
- `plugin_install_pkg` capability — `pkg add -f <url>` against an allowlist of `github.com` and `*.githubusercontent.com`.
- `force_status_push` — manual full-status trigger from the server side.
- Generic `_legacy_xml_op` engine handling all three legacy XML containers identically:
  - `legacy_filter_update`
  - `legacy_nat_{create,update,toggle,delete}` (port-forward)
  - `legacy_outbound_{create,update,toggle,delete}` (manual outbound)
- Stable hash-based identification of legacy rules — byte-identical between agent and server, survives reorderings.
- `local_api_url` setting for boxes where 443 is bound to a reverse proxy (e.g. HAProxy-fronted setups).
- Settings UI under **Services → Fleet Agent** with verify-TLS toggle, custom interval, Test Connection button.

### Fixed (cumulative)
- `cron_setup.php` uses `dom_import_simplexml` + `removeChild` instead of `unset $jobs->job[$i]` — SimpleXML doesn't reliably remove repeated children, which led to **4 parallel agent runs** when the cron got re-registered multiple times.
- `pkg add -f` (force-reinstall) instead of `pkg add -y` — the `-y` flag only exists for `pkg install`, not `pkg add`.

### Notes
This is the first **public release**. Prior versions (`0.0.1` … `0.0.10`) only ever existed inside the os-fleet monorepo and were deployed manually via `deploy-dev.sh`. They are intentionally **not** retroactively tagged here; the version number is preserved so installs that already ran the dev rsync don't suddenly think they're "outdated" relative to the published release.
