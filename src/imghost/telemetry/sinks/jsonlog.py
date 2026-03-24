from __future__ import annotations

import json
import logging

from ..models import TelemetryEvent


class JsonLogTelemetrySink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("imghost.telemetry")

    async def write(self, record: TelemetryEvent) -> None:
        self.logger.info("telemetry_event %s", json.dumps(record.to_dict(), sort_keys=True))
