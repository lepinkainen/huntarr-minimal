#!/usr/bin/env python3
"""
Huntarr Minimal - Search missing & upgrade media in Sonarr/Radarr.

A single-file script that:
  1. Reads config.yaml for Sonarr/Radarr connection details
  2. Finds missing movies/episodes and triggers searches
  3. Finds cutoff-unmet items and triggers upgrade searches
  4. Tracks state in SQLite so it doesn't re-search the same items

Usage:
  python huntarr.py                    # uses ./config.yaml
  python huntarr.py -c /path/to.yaml   # explicit config path
  python huntarr.py --dry-run           # show what would be searched, don't trigger

Future (Docker/cron):
  Mount config.yaml and huntarr.db to persistent storage.
  Call via cron: 0 */6 * * * python /app/huntarr.py -c /config/config.yaml
"""

import argparse
import datetime
import logging
import random
import sqlite3
import sys
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("huntarr")

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)-7s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")
    # Quiet down requests/urllib3
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        log.error("Config file not found: %s", path)
        sys.exit(1)
    with open(p) as f:
        cfg = yaml.safe_load(f)
    if not cfg:
        log.error("Config file is empty: %s", path)
        sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# State DB  (SQLite)
# ---------------------------------------------------------------------------
class StateDB:
    """Tracks which media IDs have been searched to avoid duplicates."""

    def __init__(self, db_path: str, ttl_hours: int = 168):
        self.ttl_hours = ttl_hours
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS searched (
                app       TEXT NOT NULL,
                instance  TEXT NOT NULL,
                media_id  TEXT NOT NULL,
                searched_at TEXT NOT NULL,
                PRIMARY KEY (app, instance, media_id)
            )
        """)
        self.conn.commit()

    def is_searched(self, app: str, instance: str, media_id: str) -> bool:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=self.ttl_hours)
        ).isoformat()
        row = self.conn.execute(
            "SELECT 1 FROM searched WHERE app=? AND instance=? AND media_id=? AND searched_at>?",
            (app, instance, media_id, cutoff),
        ).fetchone()
        return row is not None

    def mark_searched(self, app: str, instance: str, media_id: str) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO searched (app, instance, media_id, searched_at) VALUES (?,?,?,?)",
            (app, instance, media_id, now),
        )
        self.conn.commit()

    def purge_expired(self) -> int:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=self.ttl_hours)
        ).isoformat()
        cur = self.conn.execute("DELETE FROM searched WHERE searched_at <= ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Arr API helpers
# ---------------------------------------------------------------------------
class ArrClient:
    """Minimal HTTP client for *arr v3 APIs."""

    def __init__(self, name: str, url: str, api_key: str, timeout: int = 120):
        self.name = name
        self.base = url.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = self.key

    def _url(self, endpoint: str) -> str:
        return f"{self.base}/api/v3/{endpoint.lstrip('/')}"

    def get(self, endpoint: str, params: dict | None = None):
        r = self.session.get(self._url(endpoint), params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, endpoint: str, data: dict):
        r = self.session.post(self._url(endpoint), json=data, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def check_connection(self) -> bool:
        try:
            status = self.get("system/status")
            ver = status.get("version", "unknown")
            log.info("  [%s] Connected - version %s", self.name, ver)
            return True
        except Exception as e:
            log.error("  [%s] Connection failed: %s", self.name, e)
            return False


# ---------------------------------------------------------------------------
# Sonarr logic
# ---------------------------------------------------------------------------
def _parse_date(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        clean = s
        if "." in clean and "Z" in clean:
            clean = clean.split(".")[0] + "Z"
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(clean)
    except (ValueError, TypeError):
        return None


def sonarr_hunt_missing(
    client: ArrClient,
    instance: str,
    state: StateDB,
    limit: int,
    monitored_only: bool,
    skip_future: bool,
    dry_run: bool,
) -> int:
    """Find missing episodes in Sonarr via wanted/missing and trigger searches."""
    log.info("  [%s] Hunting missing episodes (limit=%d)", instance, limit)

    # Get total count first
    data = client.get("wanted/missing", params={
        "page": 1, "pageSize": 1, "includeSeries": "true", "monitored": monitored_only,
    })
    total = data.get("totalRecords", 0)
    if total == 0:
        log.info("  [%s] No missing episodes found", instance)
        return 0

    # Fetch a random page of reasonable size
    page_size = min(100, total)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = random.randint(1, total_pages)
    log.info("  [%s] %d missing episodes total, sampling page %d/%d", instance, total, page, total_pages)

    data = client.get("wanted/missing", params={
        "page": page, "pageSize": page_size, "includeSeries": "true", "monitored": monitored_only,
    })
    episodes = data.get("records", [])

    # Filter monitored
    if monitored_only:
        episodes = [
            ep for ep in episodes
            if ep.get("series", {}).get("monitored", False) and ep.get("monitored", False)
        ]

    # Skip future episodes
    now = datetime.datetime.now(datetime.timezone.utc)
    if skip_future:
        episodes = [
            ep for ep in episodes
            if not (d := _parse_date(ep.get("airDateUtc"))) or d <= now
        ]

    # Filter already-searched
    candidates = [
        ep for ep in episodes
        if not state.is_searched("sonarr", instance, str(ep["id"]))
    ]

    if not candidates:
        log.info("  [%s] All sampled episodes already searched recently", instance)
        return 0

    # Pick random subset up to limit
    to_search = random.sample(candidates, min(limit, len(candidates)))

    searched = 0
    for ep in to_search:
        ep_id = ep["id"]
        series_title = ep.get("series", {}).get("title", "?")
        season = ep.get("seasonNumber", "?")
        episode_num = ep.get("episodeNumber", "?")
        label = f"{series_title} S{season:02d}E{episode_num:02d}" if isinstance(season, int) and isinstance(episode_num, int) else f"{series_title} S{season}E{episode_num}"

        if dry_run:
            log.info("  [%s] [DRY RUN] Would search: %s (id=%s)", instance, label, ep_id)
        else:
            try:
                client.post("command", {"name": "EpisodeSearch", "episodeIds": [ep_id]})
                log.info("  [%s] Triggered search: %s", instance, label)
                state.mark_searched("sonarr", instance, str(ep_id))
                searched += 1
            except Exception as e:
                log.error("  [%s] Search failed for %s: %s", instance, label, e)

    return searched


def sonarr_hunt_upgrades(
    client: ArrClient,
    instance: str,
    state: StateDB,
    limit: int,
    monitored_only: bool,
    dry_run: bool,
) -> int:
    """Find cutoff-unmet episodes in Sonarr and trigger searches."""
    log.info("  [%s] Hunting quality upgrades (limit=%d)", instance, limit)

    # Get total count
    data = client.get("wanted/cutoff", params={
        "page": 1, "pageSize": 1, "includeSeries": "true", "monitored": monitored_only,
    })
    total = data.get("totalRecords", 0)
    if total == 0:
        log.info("  [%s] No cutoff-unmet episodes found", instance)
        return 0

    # Random page
    page_size = min(100, total)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = random.randint(1, total_pages)
    log.info("  [%s] %d cutoff-unmet episodes total, sampling page %d/%d", instance, total, page, total_pages)

    data = client.get("wanted/cutoff", params={
        "page": page, "pageSize": page_size, "includeSeries": "true", "monitored": monitored_only,
    })
    episodes = data.get("records", [])

    if monitored_only:
        episodes = [
            ep for ep in episodes
            if ep.get("series", {}).get("monitored", False) and ep.get("monitored", False)
        ]

    candidates = [
        ep for ep in episodes
        if not state.is_searched("sonarr_upgrade", instance, str(ep["id"]))
    ]

    if not candidates:
        log.info("  [%s] All sampled cutoff episodes already searched recently", instance)
        return 0

    to_search = random.sample(candidates, min(limit, len(candidates)))

    searched = 0
    for ep in to_search:
        ep_id = ep["id"]
        series_title = ep.get("series", {}).get("title", "?")
        season = ep.get("seasonNumber", "?")
        episode_num = ep.get("episodeNumber", "?")
        label = f"{series_title} S{season:02d}E{episode_num:02d}" if isinstance(season, int) and isinstance(episode_num, int) else f"{series_title} S{season}E{episode_num}"

        if dry_run:
            log.info("  [%s] [DRY RUN] Would upgrade-search: %s (id=%s)", instance, label, ep_id)
        else:
            try:
                client.post("command", {"name": "EpisodeSearch", "episodeIds": [ep_id]})
                log.info("  [%s] Triggered upgrade search: %s", instance, label)
                state.mark_searched("sonarr_upgrade", instance, str(ep_id))
                searched += 1
            except Exception as e:
                log.error("  [%s] Upgrade search failed for %s: %s", instance, label, e)

    return searched


# ---------------------------------------------------------------------------
# Radarr logic
# ---------------------------------------------------------------------------
def radarr_hunt_missing(
    client: ArrClient,
    instance: str,
    state: StateDB,
    limit: int,
    monitored_only: bool,
    skip_future: bool,
    dry_run: bool,
) -> int:
    """Find missing movies in Radarr via wanted/missing and trigger searches."""
    log.info("  [%s] Hunting missing movies (limit=%d)", instance, limit)

    # Get total via minimal query
    data = client.get("wanted/missing", params={
        "page": 1, "pageSize": 1, "monitored": monitored_only,
    })
    total = data.get("totalRecords", 0)
    if total == 0:
        log.info("  [%s] No missing movies found", instance)
        return 0

    # Random page
    page_size = min(100, total)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = random.randint(1, total_pages)
    log.info("  [%s] %d missing movies total, sampling page %d/%d", instance, total, page, total_pages)

    data = client.get("wanted/missing", params={
        "page": page, "pageSize": page_size, "monitored": monitored_only,
    })
    movies = data.get("records", [])

    if monitored_only:
        movies = [m for m in movies if m.get("monitored", False)]

    # Skip future releases
    now = datetime.datetime.now(datetime.timezone.utc)
    if skip_future:
        filtered = []
        for m in movies:
            rd = _parse_date(m.get("releaseDate") or m.get("digitalRelease") or m.get("physicalRelease"))
            if rd and rd > now:
                continue
            filtered.append(m)
        movies = filtered

    # Filter already-searched
    candidates = [
        m for m in movies
        if not state.is_searched("radarr", instance, str(m["id"]))
    ]

    if not candidates:
        log.info("  [%s] All sampled movies already searched recently", instance)
        return 0

    to_search = random.sample(candidates, min(limit, len(candidates)))

    searched = 0
    for movie in to_search:
        mid = movie["id"]
        title = movie.get("title", "?")
        year = movie.get("year", "?")
        label = f"{title} ({year})"

        if dry_run:
            log.info("  [%s] [DRY RUN] Would search: %s (id=%s)", instance, label, mid)
        else:
            try:
                client.post("command", {"name": "MoviesSearch", "movieIds": [mid]})
                log.info("  [%s] Triggered search: %s", instance, label)
                state.mark_searched("radarr", instance, str(mid))
                searched += 1
            except Exception as e:
                log.error("  [%s] Search failed for %s: %s", instance, label, e)

    return searched


def radarr_hunt_upgrades(
    client: ArrClient,
    instance: str,
    state: StateDB,
    limit: int,
    monitored_only: bool,
    dry_run: bool,
) -> int:
    """Find cutoff-unmet movies in Radarr and trigger searches."""
    log.info("  [%s] Hunting quality upgrades (limit=%d)", instance, limit)

    data = client.get("wanted/cutoff", params={
        "page": 1, "pageSize": 1, "monitored": monitored_only,
    })
    total = data.get("totalRecords", 0)
    if total == 0:
        log.info("  [%s] No cutoff-unmet movies found", instance)
        return 0

    page_size = min(100, total)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = random.randint(1, total_pages)
    log.info("  [%s] %d cutoff-unmet movies total, sampling page %d/%d", instance, total, page, total_pages)

    data = client.get("wanted/cutoff", params={
        "page": page, "pageSize": page_size, "monitored": monitored_only,
    })
    movies = data.get("records", [])

    if monitored_only:
        movies = [m for m in movies if m.get("monitored", False)]

    candidates = [
        m for m in movies
        if not state.is_searched("radarr_upgrade", instance, str(m["id"]))
    ]

    if not candidates:
        log.info("  [%s] All sampled cutoff movies already searched recently", instance)
        return 0

    to_search = random.sample(candidates, min(limit, len(candidates)))

    searched = 0
    for movie in to_search:
        mid = movie["id"]
        title = movie.get("title", "?")
        year = movie.get("year", "?")
        label = f"{title} ({year})"

        if dry_run:
            log.info("  [%s] [DRY RUN] Would upgrade-search: %s (id=%s)", instance, label, mid)
        else:
            try:
                client.post("command", {"name": "MoviesSearch", "movieIds": [mid]})
                log.info("  [%s] Triggered upgrade search: %s", instance, label)
                state.mark_searched("radarr_upgrade", instance, str(mid))
                searched += 1
            except Exception as e:
                log.error("  [%s] Upgrade search failed for %s: %s", instance, label, e)

    return searched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(cfg: dict, dry_run: bool = False) -> dict:
    """Execute one full hunting cycle. Returns summary counts."""
    state_cfg = cfg.get("state", {})
    db_path = state_cfg.get("database", "./huntarr.db")
    ttl = state_cfg.get("ttl_hours", 168)

    state = StateDB(db_path, ttl_hours=ttl)

    # Purge expired entries
    purged = state.purge_expired()
    if purged:
        log.info("Purged %d expired state entries (older than %dh)", purged, ttl)

    totals = {"sonarr_missing": 0, "sonarr_upgrades": 0, "radarr_missing": 0, "radarr_upgrades": 0}

    # --- Sonarr ---
    for inst_cfg in cfg.get("sonarr", []):
        name = inst_cfg.get("name", "Sonarr")
        url = inst_cfg.get("url", "")
        key = inst_cfg.get("api_key", "")
        if not url or not key:
            log.warning("[%s] Missing url or api_key, skipping", name)
            continue

        log.info("[SONARR] Processing instance: %s", name)
        client = ArrClient(name, url, key)
        if not client.check_connection():
            continue

        hunt_missing = inst_cfg.get("hunt_missing", 5)
        hunt_upgrades = inst_cfg.get("hunt_upgrades", 0)
        monitored = inst_cfg.get("monitored_only", True)
        skip_future = inst_cfg.get("skip_future", True)

        if hunt_missing > 0:
            totals["sonarr_missing"] += sonarr_hunt_missing(
                client, name, state, hunt_missing, monitored, skip_future, dry_run,
            )

        if hunt_upgrades > 0:
            totals["sonarr_upgrades"] += sonarr_hunt_upgrades(
                client, name, state, hunt_upgrades, monitored, dry_run,
            )

    # --- Radarr ---
    for inst_cfg in cfg.get("radarr", []):
        name = inst_cfg.get("name", "Radarr")
        url = inst_cfg.get("url", "")
        key = inst_cfg.get("api_key", "")
        if not url or not key:
            log.warning("[%s] Missing url or api_key, skipping", name)
            continue

        log.info("[RADARR] Processing instance: %s", name)
        client = ArrClient(name, url, key)
        if not client.check_connection():
            continue

        hunt_missing = inst_cfg.get("hunt_missing", 5)
        hunt_upgrades = inst_cfg.get("hunt_upgrades", 0)
        monitored = inst_cfg.get("monitored_only", True)
        skip_future = inst_cfg.get("skip_future", True)

        if hunt_missing > 0:
            totals["radarr_missing"] += radarr_hunt_missing(
                client, name, state, hunt_missing, monitored, skip_future, dry_run,
            )

        if hunt_upgrades > 0:
            totals["radarr_upgrades"] += radarr_hunt_upgrades(
                client, name, state, hunt_upgrades, monitored, dry_run,
            )

    state.close()
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Huntarr Minimal - search missing & upgrade media in Sonarr/Radarr",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be searched without triggering")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.dry_run:
        log.info("=== DRY RUN MODE - no searches will be triggered ===")

    cfg = load_config(args.config)
    totals = run(cfg, dry_run=args.dry_run)

    log.info("=== Run complete ===")
    log.info("  Sonarr missing searched:  %d", totals["sonarr_missing"])
    log.info("  Sonarr upgrades searched: %d", totals["sonarr_upgrades"])
    log.info("  Radarr missing searched:  %d", totals["radarr_missing"])
    log.info("  Radarr upgrades searched: %d", totals["radarr_upgrades"])


if __name__ == "__main__":
    main()
