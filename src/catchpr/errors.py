"""Typed errors. CLI maps these to exit codes."""

from __future__ import annotations


class CatchprError(Exception):
    """Base error for expected failures."""


class GitError(CatchprError):
    """A git command failed."""


class ProviderError(CatchprError):
    """The model provider failed or is misconfigured."""


class ParseError(CatchprError):
    """A model response was not usable JSON."""


class GitHubError(CatchprError):
    """The GitHub API rejected a comment or artifact request."""


class UsageError(CatchprError):
    """The invocation is invalid."""
