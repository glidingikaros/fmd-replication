from __future__ import annotations

import sys
from typing import Any

from fmd.core.json_io import json_text


def write_json_stdout(payload: Any) -> None:
    sys.stdout.write(json_text(payload, sort_keys=True))
