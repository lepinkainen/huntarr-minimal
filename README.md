# Huntarr Minimal

100% [vibe-engineered](https://simonwillison.net/2025/Oct/7/vibe-engineering/) from the original Huntarr project for my own needs, I ripped out all the non-essential features (for me) and left just the core functionality: searching for missing and upgrading items in Sonarr/Radarr, with a simple SQLite db to track state to avoid duplicate searches.

It can be run either with a single Python file or as a Docker container with a mounted config directory. The container includes a cron scheduler, so it will run on the hour by default after the initial run.

Even though this is fully vibed with Claude Opus 4.6 & [Pi](https://pi.dev)+GPT-5.3 Codex, I can pretty much guarantee there are no security issues like in the original project, there are no public APIs, no logins or web servers, thus the attack surface is effectively zero. It's just a script that runs and talks to your local Sonarr/Radarr instances.

I also have zero intention of adding extra features to this beyond "search missing stuff and upgrade", it does what I need and nothing else. If you want a web UI or additional integrations, the Fork button is in the top right corner 😁 [Grugbrain dev](https://grugbrain.dev) says "complexity _very_, _very_ bad" and I agree.

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

## Docker / Compose

The container now includes its own cron scheduler.

Behavior on container start:

1. Validates `/config/config.yaml` (fails fast if missing/invalid)
2. Runs one immediate hunt pass
3. Starts cron (`0 * * * *`) for hourly runs

### Docker Compose (recommended)

`compose.yml` maps `./data` on the host to `/config` in the container.

```bash
# 1. Prepare persistent config/data
mkdir -p data
cp config.yaml.example data/config.yaml
# edit data/config.yaml

# 2. Build and start
docker compose up -d --build

# 3. Follow logs
docker compose logs -f
```

### Plain docker

```bash
docker build -t huntarr-minimal .
docker run -d \
  --name huntarr-minimal \
  --restart unless-stopped \
  -v "$(pwd)/data:/config" \
  huntarr-minimal
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
