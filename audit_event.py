import json
import sys
from pathlib import Path

raw = sys.stdin.read()
try:
    event = json.loads(raw)
except json.JSONDecodeError:
    event = {"raw": raw}

log = Path(".state/audit.log")
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(event, ensure_ascii=False) + "\n")
