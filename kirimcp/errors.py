"""One error type for the whole tool, with a code a program can branch on.

Every layer above this one raises `KiriMcpError` instead of its own
exception type, so the server turns any failure into one result shape
instead of one shape per layer.
"""


class KiriMcpError(Exception):
    """A failure with a stable machine-readable code and a human message.

    The code is what a program branches on; the message is what a person
    reads. `str(e)` is the message, so the server can surface it as is.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self):
        return self.message

    def as_dict(self):
        """The error as the server reports it: the code and the message."""
        return {"code": self.code, "message": self.message}


# The codes this tool uses. A caller can branch on them, so they are
# spelled out here rather than scattered as string literals.
UNSUPPORTED_PLATFORM = "unsupported_platform"
UNSUPPORTED_OS = "unsupported_os"
FEED_UNREACHABLE = "feed_unreachable"
FEED_INVALID = "feed_invalid"
FEED_INSECURE = "feed_insecure"
KIRI_NOT_FOUND = "kiri_not_found"
NOT_INSTALLED = "not_installed"
CHECKSUM_MISMATCH = "checksum_mismatch"
SIGNATURE_INVALID = "signature_invalid"
NOT_NOTARIZED = "not_notarized"
WRONG_ARCHITECTURE = "wrong_architecture"
VERSION_MISMATCH = "version_mismatch"
UNSAFE_ARCHIVE = "unsafe_archive"
INSTALL_FAILED = "install_failed"
MODE_CONFLICT = "mode_conflict"
DOWNLOAD_FAILED = "download_failed"
TIMEOUT = "timeout"
NEEDS_ROOT = "needs_root"
