"""S3-style errors, rendered as XML error documents by the server layer."""


PRECONDITION_MSG = "At least one of the pre-conditions you specified did not hold."


class S3Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"{code}: {message}")
