"""
NAS inotify watcher — monitors directory groups and POSTs to a VM receiver
when filesystem events occur. Events are debounced per group so a burst of
uploads triggers exactly one webhook call.
"""

import logging
import os
import sys
import threading
import time

import inotify_simple
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("watcher")

# Inotify event flags we care about
WATCH_FLAGS = (
    inotify_simple.flags.CLOSE_WRITE   # file write completed
    | inotify_simple.flags.MOVED_TO    # file/dir moved into watched path
    | inotify_simple.flags.MOVED_FROM  # file/dir moved out
    | inotify_simple.flags.DELETE      # file/dir deleted
    | inotify_simple.flags.CREATE      # file/dir created (also used for new-dir tracking)
    | inotify_simple.flags.ATTRIB      # metadata changed (chmod, chown, xattr, …)
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def _dirname(path: str) -> str:
    """Return the final component of a path (directory name only)."""
    return os.path.basename(path.rstrip("/"))


class GroupWatcher:
    """Debounced webhook caller for one capture group."""

    def __init__(self, name: str, webhook_url: str, debounce_seconds: float, secret: str):
        self.name = name
        self.webhook_url = webhook_url
        self.debounce_seconds = debounce_seconds
        self._secret = secret
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_event(self, full_path: str, event_flags: list[str]) -> None:
        log.debug("[%s] %s → %s", self.name, event_flags, full_path)
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        log.info("[%s] Debounce expired — firing webhook: %s", self.name, self.webhook_url)
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        try:
            resp = requests.post(
                self.webhook_url,
                json={"group": self.name},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            log.info("[%s] Webhook OK (%d)", self.name, resp.status_code)
        except requests.RequestException as exc:
            log.error("[%s] Webhook failed: %s", self.name, exc)


class INotifyWatcher:
    """Manages inotify file descriptors and dispatches events to GroupWatchers."""

    def __init__(
        self,
        groups: list[GroupWatcher],
        group_paths: dict[str, list[str]],
        exclude_dirs: list[str],
    ):
        self._inotify = inotify_simple.INotify()
        # wd → (GroupWatcher, abs_path)
        self._wd_map: dict[int, tuple[GroupWatcher, str]] = {}
        # abs_path → wd  (to avoid duplicate watches)
        self._path_map: dict[str, int] = {}
        self._groups = {g.name: g for g in groups}
        self._group_paths = group_paths
        self._exclude_dirs = exclude_dirs

    def _is_excluded(self, name: str) -> bool:
        return name in self._exclude_dirs

    def _add_watch(self, path: str, group: GroupWatcher) -> None:
        if path in self._path_map:
            return
        if self._is_excluded(_dirname(path)):
            return
        try:
            wd = self._inotify.add_watch(path, WATCH_FLAGS)
            self._wd_map[wd] = (group, path)
            self._path_map[path] = wd
            log.info("Watching [%s] %s", group.name, path)
        except OSError as exc:
            log.warning("Cannot watch %s: %s", path, exc)

    def _add_recursive(self, root: str, group: GroupWatcher) -> None:
        """Walk root and add inotify watches for every subdirectory."""
        if self._is_excluded(_dirname(root)):
            return
        self._add_watch(root, group)
        for dirpath, dirnames, _ in os.walk(root):
            # Prune excluded names in-place so os.walk won't descend into them.
            # d is already just the directory name — direct comparison, no path splitting.
            dirnames[:] = [d for d in dirnames if not self._is_excluded(d)]
            for d in dirnames:
                self._add_watch(os.path.join(dirpath, d), group)

    def setup(self) -> None:
        for group_name, paths in self._group_paths.items():
            group = self._groups[group_name]
            for path in paths:
                if not os.path.isdir(path):
                    log.warning("[%s] Watch path does not exist (will skip): %s", group_name, path)
                    continue
                self._add_recursive(path, group)

    def run_forever(self) -> None:
        log.info("Event loop started")
        while True:
            try:
                events = self._inotify.read(timeout=5000)
            except Exception as exc:
                log.error("inotify read error: %s", exc)
                time.sleep(1)
                continue

            for event in events:
                if event.wd not in self._wd_map:
                    continue

                group, watch_path = self._wd_map[event.wd]
                flags = inotify_simple.flags.from_mask(event.mask)
                flag_names = [f.name for f in flags]
                filename = event.name or ""
                full_path = os.path.join(watch_path, filename) if filename else watch_path

                # Auto-register newly created subdirectories so watching stays recursive
                is_dir = bool(event.mask & inotify_simple.flags.ISDIR)
                is_create = bool(event.mask & inotify_simple.flags.CREATE)
                if is_dir and is_create and filename:
                    if not self._is_excluded(filename):
                        self._add_recursive(full_path, group)

                group.on_event(full_path, flag_names)


def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    cfg = load_config(config_path)
    secret = cfg.get("shared_secret", "")

    # Directory names to never watch — saves inotify watches and avoids noise.
    # @eaDir  = Synology thumbnail/metadata dirs (one per photo directory)
    # #recycle = Synology recycle bin
    default_excludes = ["@eaDir", "#recycle", "@tmp", ".recycle"]
    exclude_dirs = cfg.get("exclude_dirs", default_excludes)
    log.info("Excluding directory names: %s", exclude_dirs)

    groups: list[GroupWatcher] = []
    group_paths: dict[str, list[str]] = {}

    for g in cfg.get("groups", []):
        name = g["name"]
        watcher = GroupWatcher(
            name=name,
            webhook_url=g["webhook_url"],
            debounce_seconds=float(g.get("debounce_seconds", 30)),
            secret=secret,
        )
        groups.append(watcher)
        group_paths[name] = [os.path.normpath(p) for p in g.get("paths", [])]

    if not groups:
        log.error("No groups defined in config")
        sys.exit(1)

    watcher = INotifyWatcher(groups, group_paths, exclude_dirs)
    watcher.setup()
    watcher.run_forever()


if __name__ == "__main__":
    main()
