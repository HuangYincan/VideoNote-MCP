from enum import IntEnum


class StatusCode(IntEnum):
    SUCCESS = 0
    FAIL = 1

    DOWNLOAD_ERROR = 1001
    TRANSCRIBE_ERROR = 1002
    GENERATE_ERROR = 1003

    INVALID_URL = 2001
    PARAM_ERROR = 2002