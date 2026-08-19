from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

DAILY_NEW_RECORD_LIMIT = 1_000
DEFAULT_RUN_LIMIT = 200
_DEFAULT_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    export_dir: Path = Path("exports")
    timezone: ZoneInfo = _DEFAULT_TIMEZONE

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = (
            Path(os.environ["MAPSLEAD_DATA_DIR"])
            if "MAPSLEAD_DATA_DIR" in os.environ
            else Path("data")
        )
        export_dir = (
            Path(os.environ["MAPSLEAD_EXPORT_DIR"])
            if "MAPSLEAD_EXPORT_DIR" in os.environ
            else Path("exports")
        )
        return cls(data_dir=data_dir, export_dir=export_dir)
