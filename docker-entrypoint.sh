#!/bin/sh
set -eu

echo "[huntarr-minimal] Container starting..."
echo "[huntarr-minimal] Validating /config/config.yaml..."
python3 - <<'PY'
from pathlib import Path
import sys
import yaml

cfg_path = Path('/config/config.yaml')
if not cfg_path.exists():
    print('[huntarr-minimal] ERROR: Missing config file: /config/config.yaml', file=sys.stderr)
    sys.exit(1)

try:
    cfg = yaml.safe_load(cfg_path.read_text())
except Exception as e:
    print(f'[huntarr-minimal] ERROR: Invalid YAML in /config/config.yaml: {e}', file=sys.stderr)
    sys.exit(1)

if not isinstance(cfg, dict) or not cfg:
    print('[huntarr-minimal] ERROR: /config/config.yaml is empty or not a YAML mapping', file=sys.stderr)
    sys.exit(1)

instances = []
for app in ('sonarr', 'radarr'):
    value = cfg.get(app, [])
    if value is None:
        value = []
    if not isinstance(value, list):
        print(f'[huntarr-minimal] ERROR: "{app}" must be a list', file=sys.stderr)
        sys.exit(1)
    for i, inst in enumerate(value, start=1):
        if not isinstance(inst, dict):
            print(f'[huntarr-minimal] ERROR: {app}[{i}] must be an object', file=sys.stderr)
            sys.exit(1)
        name = inst.get('name', f'{app}[{i}]')
        if not inst.get('url') or not inst.get('api_key'):
            print(f'[huntarr-minimal] ERROR: {name} is missing required "url" or "api_key"', file=sys.stderr)
            sys.exit(1)
        instances.append(name)

if not instances:
    print('[huntarr-minimal] ERROR: No Sonarr/Radarr instances configured', file=sys.stderr)
    sys.exit(1)

print(f'[huntarr-minimal] Config validation passed ({len(instances)} instance(s)).')
PY

echo "[huntarr-minimal] Running initial huntarr pass..."
python3 /app/huntarr.py -c /config/config.yaml
echo "[huntarr-minimal] Initial huntarr pass completed successfully."

echo "[huntarr-minimal] Cron schedule loaded from /etc/cron.d/huntarr"
echo "[huntarr-minimal] Huntarr will run according to cron settings /config/config.yaml"

exec "$@"
