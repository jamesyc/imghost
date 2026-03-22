from __future__ import annotations

from fastapi import HTTPException


def validate_pagination(limit: int, offset: int, *, max_limit: int = 200) -> None:
    if limit < 1 or limit > max_limit:
        raise HTTPException(status_code=400, detail=f"limit must be between 1 and {max_limit}.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative.")
