"""410 responses for ONT/OLT/ACS write routes retired by the single-writer rule."""

from __future__ import annotations

import json

from fastapi.responses import JSONResponse

CONFIGURE_GUIDANCE = (
    "This direct device write was retired: the ONT configuration lifecycle is the "
    "only writer. Use the ONT's Configure tab instead."
)


def retired_writer_response(
    action: str, guidance: str = CONFIGURE_GUIDANCE
) -> JSONResponse:
    message = f"{action} is retired. {guidance}"
    return JSONResponse(
        {"success": False, "message": message, "retired": True},
        status_code=410,
        headers={
            "HX-Trigger": json.dumps(
                {"showToast": {"message": message, "type": "error"}}
            )
        },
    )
