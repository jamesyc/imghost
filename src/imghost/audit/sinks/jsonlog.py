from __future__ import annotations

import json
import logging

from ..models import AuditRecord


class JsonLogAuditSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("imghost.audit")

    async def write(self, record: AuditRecord) -> None:
        self.logger.info("audit_event %s", json.dumps(record.to_dict(), sort_keys=True))
