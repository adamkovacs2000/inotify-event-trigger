# inotify-event-trigger

Watches folders on the storage device for filesystem changes and automatically triggers library scans on services running on a separate VM (Immich, Plex, etc.).

## How it works

```
NAS (Storage server)                        VM (Ubuntu)
┌─────────────────────┐               ┌─────────────────────┐
│  inotify-watcher    │  HTTP POST    │  inotify-trigger    │
│                     │ ──────────►   │                     │
│  watches folders    │               │  calls Immich /     │
│  debounces events   │               │  Plex APIs          │
└─────────────────────┘               └─────────────────────┘
```

- **inotify-watcher** — runs on the storage device, watches configured directories using Linux inotify, debounces bursts of events (e.g. 1000-file import → one webhook call), then POSTs to the trigger.
- **inotify-trigger** — runs on the VM, receives webhooks and calls the appropriate service APIs.

## Setup

### Storage server (inotify-watcher)

```bash
cd inotify-watcher
cp config.yaml.example config.yaml   # edit paths, webhook URL, shared secret
docker compose up -d --build
```

If you hit inotify watch limit errors:
```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee /etc/sysctl.d/99-inotify.conf
sudo sysctl -p /etc/sysctl.d/99-inotify.conf
```

### Services server (inotify-trigger)

```bash
cd inotify-trigger
cp config.yaml.example config.yaml   # edit API keys, shared secret
docker compose up -d --build
```

## Configuration

Both services share a `shared_secret` that must match — the watcher sends it as a Bearer token, the trigger validates it.

**inotify-watcher/config.yaml** — define watch groups:
```yaml
shared_secret: "your-secret"

groups:
  - name: pictures
    paths:
      - /volume1/photo
    debounce_seconds: 30
    webhook_url: "http://<vm-ip>:64123/trigger/pictures"
```

**inotify-trigger/config.yaml** — define what each group triggers:
```yaml
shared_secret: "your-secret"

handlers:
  pictures:
    - type: immich_scan
      url: "http://immich:2283"
      api_key: "your-immich-api-key"

  movies:
    - type: plex_refresh
      url: "http://plex:32400"
      token: "your-plex-token"
      section_id: "1"
```

### Supported handler types

| Type | Description |
|------|-------------|
| `immich_scan` | Triggers an Immich external library scan |
| `plex_refresh` | Refreshes a Plex library section |

## Health check

```bash
curl http://<vm-ip>:64123/health
```
