from __future__ import annotations

"""Compatibility shim for `src.er2data.er2query` CLI entrypoint."""

from src.er2data import er2query as _impl
from src.er2data.er2query import *  # noqa: F401,F403


def main() -> None:
    _impl.main()

if __name__ == "__main__":
    main()
