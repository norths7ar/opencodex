"""Transport-independent failures of a management operation."""


class ApplicationError(ValueError):
    def __init__(self, status_code: int, detail: str, *, code: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code or {
            400: "invalid_input",
            404: "not_found",
            409: "conflict",
            422: "invalid_input",
            503: "unavailable",
            504: "timeout",
        }.get(status_code, "operation_failed")
