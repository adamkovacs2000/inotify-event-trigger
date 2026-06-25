# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Two Docker containers that bridge filesystem events on a storage server (Synology NAS) to service API calls on a separate VM:

- **inotify-watcher** (runs on NAS): watches directories via Linux inotify, debounces event bursts, POSTs to the trigger over HTTP with a Bearer token
- **inotify-trigger** (runs on VM): Flask HTTP server that receives webhooks and dispatches to service APIs (Immich, Plex)

## Deploy & run

Each component is self-contained — deploy from its own directory:

```bash
# On the NAS
cd inotify-watcher && docker compose up -d --build

# On the VM
cd inotify-trigger && docker compose up -d --build
```

Config is a volume-mounted file read at startup. **Config changes only need a restart; code changes need a rebuild.**

```bash
docker restart inotify-watcher   # config-only change
docker compose up -d --build     # code change
```

Logs:
```bash
docker logs -f inotify-watcher
docker logs -f inotify-trigger
```

Test the trigger end-to-end (from any machine on the LAN):
```bash
curl http://<vm-ip>:64123/health
curl -X POST http://<vm-ip>:64123/trigger/<group> \
  -H "Authorization: Bearer <shared_secret>" \
  -H "Content-Type: application/json" -d "{}"
```

## Architecture

### inotify-watcher

`watcher.py` has two main classes:

- **`GroupWatcher`** — one instance per config group. Holds a `threading.Timer` that resets on every event and fires the webhook only after `debounce_seconds` of silence. This collapses thousands of file events from a bulk import into a single API call.
- **`INotifyWatcher`** — manages inotify watch descriptors. Walks directories recursively at startup and auto-registers new subdirs created at runtime (`CREATE | ISDIR` events). Exclusion check uses **direct name comparison** (`d in exclude_dirs`) against `os.walk`'s `dirnames` — do not replace this with path-splitting logic, it silently breaks on Synology.

Watched events: `CLOSE_WRITE`, `MOVED_TO`, `MOVED_FROM`, `DELETE`, `CREATE`, `ATTRIB`.

### inotify-trigger

`trigger.py` is a Flask app with a single route `POST /trigger/<group_name>`. Config maps group names to ordered lists of handlers. All handlers in a group run sequentially; partial failures return HTTP 207.

Adding a new handler type: add an `if htype == "..."` branch in `_dispatch()` and implement the function.

### Config relationship

Both services share `shared_secret`. The group `name` in `inotify-watcher/config.yaml` must match the handler key in `inotify-trigger/config.yaml`, and must match the URL path (`/trigger/<name>`).

## Synology-specific notes

- **`network_mode: host`** is required in `inotify-watcher/docker-compose.yml` — Docker's bridge network on Synology cannot reach LAN hosts.
- **`fs.inotify.max_user_watches`** default (8192) is exhausted by large photo libraries. Set to 524288 in `/etc/sysctl.d/99-inotify.conf` on the NAS host.
- **`@eaDir`** directories (Synology per-folder thumbnail cache) must stay in `exclude_dirs` — they mirror every photo directory and consume the entire watch budget.
- Volume mounts use identical host and container paths (e.g. `/volume1/photo:/volume1/photo:ro`) so config.yaml paths are the real NAS paths with no translation.

## Adding a new handler type

1. Add an `if htype == "your_type":` branch in `_dispatch()` in `trigger.py`
2. Implement `_your_type(h: dict) -> dict` — raise on failure, return a dict on success
3. Document the required/optional config keys in the docstring
4. Add an example to `inotify-trigger/config.yaml.example`
