"""Application-level exceptions. Messages are user-facing (Czech)."""


class PollEdgeError(Exception):
    """Base class for errors that the CLI reports without a traceback."""


class ConfigError(PollEdgeError):
    pass


class FetchError(PollEdgeError):
    pass


class TableStructureError(PollEdgeError):
    """Wikipedia table does not match the configured structure."""
