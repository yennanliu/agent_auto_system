# Debug

## Cmd

```bash

# run app

ALLOW_INSECURE_DEFAULTS=1 uv run uvicorn src.main:app --reload --port 8000  # dev only: admin/admin + dev secret


# get tasker login token
uv run python scripts/tasker_login.py
```