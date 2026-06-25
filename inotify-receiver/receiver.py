"""
VM event receiver — listens for webhook POSTs from the NAS watcher and
dispatches them to the appropriate service APIs (Immich, Plex, …).
"""

import logging
import os
import sys
from functools import wraps

import requests
import yaml
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("receiver")

app = Flask(__name__)
_config: dict = {}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


# ── Auth middleware ───────────────────────────────────────────────────────────

def require_secret(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        secret = _config.get("shared_secret", "")
        if secret:
            auth = request.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ").strip()
            if token != secret:
                log.warning("Rejected request with bad/missing token from %s", request.remote_addr)
                return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/trigger/<group_name>")
@require_secret
def trigger(group_name: str):
    handlers = _config.get("handlers", {}).get(group_name)
    if handlers is None:
        log.warning("Unknown group: %s", group_name)
        return jsonify({"error": f"Unknown group: {group_name}"}), 404

    log.info("Trigger received for group: %s", group_name)
    results = []
    for handler in handlers:
        htype = handler.get("type", "unknown")
        try:
            result = _dispatch(handler)
            log.info("[%s] Handler %s succeeded: %s", group_name, htype, result)
            results.append({"type": htype, "status": "ok", "detail": result})
        except Exception as exc:
            log.error("[%s] Handler %s failed: %s", group_name, htype, exc)
            results.append({"type": htype, "status": "error", "detail": str(exc)})

    all_ok = all(r["status"] == "ok" for r in results)
    return jsonify({"group": group_name, "results": results}), 200 if all_ok else 207


# ── Handler dispatch ──────────────────────────────────────────────────────────

def _dispatch(handler: dict):
    htype = handler["type"]
    if htype == "immich_scan":
        return _immich_scan(handler)
    if htype == "plex_refresh":
        return _plex_refresh(handler)
    raise ValueError(f"Unknown handler type: {htype!r}")


def _immich_scan(h: dict) -> dict:
    """
    Trigger an Immich external-library scan.

    Required config keys:
      url        - base URL, e.g. http://immich:2283
      api_key    - Immich API key

    Optional:
      library_id - scan a specific library; omit to scan ALL external libraries
      refresh_all_files   - bool, default false
      refresh_modified    - bool, default true
    """
    base = h["url"].rstrip("/")
    api_key = h["api_key"]
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "refreshAllFiles": h.get("refresh_all_files", False),
        "refreshModifiedFiles": h.get("refresh_modified", True),
    }

    library_id = h.get("library_id")
    if library_id:
        url = f"{base}/api/libraries/{library_id}/scan"
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return {"library_id": library_id, "http_status": resp.status_code}
    else:
        libs_resp = requests.get(f"{base}/api/libraries", headers=headers, timeout=15)
        libs_resp.raise_for_status()
        all_libraries = libs_resp.json()
        log.info("Immich libraries found: %s", [(l.get("id"), l.get("type"), l.get("name")) for l in all_libraries])

        # Optional filter — set library_type: "EXTERNAL" in config to restrict.
        # Default: scan all libraries so version differences in the type field don't matter.
        library_type = h.get("library_type")
        libraries = [lib for lib in all_libraries if lib.get("type") == library_type] if library_type else all_libraries

        if not libraries:
            return {"message": "No libraries found", "returned_by_api": all_libraries}
        for lib in libraries:
            scan_resp = requests.post(
                f"{base}/api/libraries/{lib['id']}/scan",
                json=payload,
                headers=headers,
                timeout=30,
            )
            scan_resp.raise_for_status()
        return {"scanned_libraries": [lib["id"] for lib in libraries]}


def _plex_refresh(h: dict) -> dict:
    """
    Trigger a Plex library section refresh.

    Required config keys:
      url        - base URL, e.g. http://plex:32400
      token      - Plex authentication token
      section_id - library section ID (find it in Plex → Settings → Libraries)

    Optional:
      force      - bool, default false (true = deep scan / force metadata refresh)
    """
    base = h["url"].rstrip("/")
    token = h["token"]
    section_id = h["section_id"]
    force = h.get("force", False)

    params = {"X-Plex-Token": token}
    if force:
        params["force"] = 1

    url = f"{base}/library/sections/{section_id}/refresh"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return {"section_id": section_id, "http_status": resp.status_code}


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    global _config
    _config = load_config(config_path)

    port = int(os.environ.get("PORT", 8080))
    log.info("Starting receiver on :%d", port)
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
