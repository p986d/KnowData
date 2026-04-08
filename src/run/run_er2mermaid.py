from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.er_json_to_mermaid import ERJsonToMermaidConverter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert ER JSON to Mermaid ER diagram code."
    )
    parser.add_argument(
        "--input-path",
        default=str(
            PROJECT_ROOT
            / "metadata"
            / "sf_bq050_20260330-154746"
            / "nl2er_output.json"
        ),
        help="Path to the ER JSON file.",
    )
    parser.add_argument(
        "--output-path",
        help="Optional output file path. When omitted, print Mermaid code to stdout.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    converter = ERJsonToMermaidConverter()
    mermaid_code = converter.convert_file(args.input_path)

    if args.output_path:
        output_path = Path(args.output_path)
        output_path.write_text(mermaid_code, encoding="utf-8")
        print(f"Mermaid code written to: {output_path}")
        return 0

    print(mermaid_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
