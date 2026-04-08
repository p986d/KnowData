from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


def _resolve_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    else:
        resolved = resolved.resolve()
    return resolved


def build_timestamp() -> str:
    return datetime.now().strftime(TIMESTAMP_FORMAT)


def format_elapsed_seconds(elapsed_seconds: float) -> str:
    return f"{max(0.0, float(elapsed_seconds)):.2f}s"


def emit_step_done_log(
    *,
    prefix: str,
    step: str,
    elapsed_seconds: float,
    ok: bool = True,
    **summary: object,
) -> None:
    parts = [
        f"[{prefix}]",
        f"step={step}",
        f"ok={str(bool(ok))}",
        f"elapsed={format_elapsed_seconds(elapsed_seconds)}",
    ]

    for key, value in summary.items():
        if value is None:
            continue
        if isinstance(value, str):
            text = " ".join(value.split())
            if not text:
                continue
            parts.append(f"{key}={text}")
            continue
        parts.append(f"{key}={value}")

    print(" ".join(parts), flush=True)


def resolve_run_dir(
    *,
    run_prefix: str,
    run_dir: str | Path | None,
    run_root: str | Path,
    timestamp: str | None = None,
) -> Path:
    if run_dir is not None:
        resolved = _resolve_path(run_dir)
    else:
        resolved_root = _resolve_path(run_root)
        resolved = resolved_root / f"{run_prefix}_{timestamp or build_timestamp()}"

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_run_log_dir(
    *,
    run_prefix: str,
    log_dir: str | Path | None,
    log_root: str | Path,
    timestamp: str | None = None,
) -> Path:
    return resolve_run_dir(
        run_prefix=run_prefix,
        run_dir=log_dir,
        run_root=log_root,
        timestamp=timestamp,
    )


def resolve_output_path(
    *,
    output_path: str | Path | None,
    run_dir: str | Path,
    default_filename: str,
) -> Path:
    if output_path is not None:
        resolved = _resolve_path(output_path)
    else:
        resolved = _resolve_path(run_dir) / default_filename

    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    output_path = _resolve_path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path
