# Huntarr Minimal

A single-file Python script (~400 lines) that searches for missing and quality-upgrade media in Sonarr and Radarr. No web UI, no database server, no framework dependencies.

## Requirements

- Python 3.10+
- `requests` and `pyyaml`

### Option A: pip

```bash
pip install requests pyyaml
```

### Option B: uv (recommended)

`huntarr-minimal/` includes a `pyproject.toml` with dependencies, so you can run directly with uv:

```bash
uv run python3 huntarr.py --dry-run
```

(Equivalent to creating/using an isolated env with the required packages.)

## Quick Start

```bash
# 1. Create your config
cp config.yaml.example config.yaml
# Edit config.yaml with your Sonarr/Radarr URLs and API keys

# 2. Dry run (see what would be searched)
python huntarr.py --dry-run

# 3. Run for real
python huntarr.py

# 4. Verbose output
python huntarr.py -v
```

## How It Works

Each run:

1. Connects to each configured Sonarr/Radarr instance
2. Queries `wanted/missing` for missing content (random page sampling for large libraries)
3. Queries `wanted/cutoff` for quality upgrades (if enabled)
4. Filters out future releases, already-searched items (via SQLite state)
5. Triggers search commands via the *arr API
6. Records searched items in `huntarr.db` to avoid duplicates for `ttl_hours`

The script runs once and exits. Schedule it with cron or a Docker cron container.

## Docker (Unraid / future)

```bash
docker build -t huntarr-minimal .
docker run --rm -v /mnt/user/appdata/huntarr-minimal:/config huntarr-minimal
```

For Unraid, mount `/config` to persistent storage. Add to cron via User Scripts plugin:

```bash
docker run --rm -v /mnt/user/appdata/huntarr-minimal:/config huntarr-minimal
```

## Config Reference

See `config.yaml.example` for all options. Key settings:

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `hunt_missing` | 5 | Max items to search per instance per run |
| `hunt_upgrades` | 0 | Max upgrade searches per run (0 = disabled) |
| `monitored_only` | true | Only search monitored content |
| `skip_future` | true | Skip unreleased content |
| `state.ttl_hours` | 168 | Remember searched items for 7 days |

## State Database

`huntarr.db` is a SQLite file that tracks which items have been searched. This prevents the script from re-searching the same content every run and getting you rate-limited by your indexer.

Delete it to reset all state and start fresh.
