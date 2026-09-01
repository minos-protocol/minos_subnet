"""Minos genomics utilities with dependency-isolated lazy exports.

Importing a lightweight utility (for example the private replay tokenizer)
must not initialize wallet/network dependencies. Runtime users can keep the
existing ``from utils import ScoreTracker`` style; the requested symbol is
loaded on first access.
"""

from importlib import import_module


_EXPORTS = {
    # Scoring
    "HappyScorer": (".scoring", "HappyScorer"),
    "AdvancedScorer": (".scoring", "AdvancedScorer"),
    # Weight tracking
    "ScoreTracker": (".weight_tracking", "ScoreTracker"),
    # File/path utilities
    "download_file": (".file_utils", "download_file"),
    "safe_round_dir_name": (".path_utils", "safe_round_dir_name"),
    # Platform client
    "PlatformConfig": (".platform_client", "PlatformConfig"),
    "PlatformClient": (".platform_client", "PlatformClient"),
    "MinerPlatformClient": (".platform_client", "MinerPlatformClient"),
    "ValidatorPlatformClient": (".platform_client", "ValidatorPlatformClient"),
    "PlatformClientError": (".platform_client", "PlatformClientError"),
    "AuthenticationError": (".platform_client", "AuthenticationError"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
