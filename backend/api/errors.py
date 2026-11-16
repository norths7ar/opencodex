"""One error envelope for all management transports, including session failures."""

from fastapi.responses import JSONResponse


def error_body(code, message, *, retryable=False, fields=None):
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            **({"fields": fields} if fields else {}),
        }
    }


def error_response(status, code, message, *, context=None, retryable=False, details=None):
    body = error_body(code, message, retryable=retryable)
    body["error"].update(details or {})
    return JSONResponse({**(context or {}), **body}, status)
