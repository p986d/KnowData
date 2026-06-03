from __future__ import annotations

__all__ = ["BuildTopicRunner"]


def __getattr__(name: str):
    if name == "BuildTopicRunner":
        from src.buildtopic.pipeline import BuildTopicRunner

        return BuildTopicRunner
    raise AttributeError(name)
