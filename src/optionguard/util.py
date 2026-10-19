"""JSON-safety helper -- numpy scalars, dates and dataclasses aren't
directly JSON-serializable; everything stored in a JSONB column or returned
from the API goes through this first."""
from __future__ import annotations

import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal

import numpy as np


def to_jsonable(obj):
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) else obj
    if isinstance(obj, (np.floating,)):
        val = float(obj)
        return None if math.isnan(val) else val
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
