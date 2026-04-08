from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any


class ERJsonToMermaidConverter:
    """Convert ER-model JSON into Mermaid ER diagram code and preview files."""

    _NON_WORD_RE = re.compile(r"[^0-9A-Za-z_]+")

    def __init__(self, show_relationship_labels: bool = False) -> None:
        self.show_relationship_labels = show_relationship_labels

    def convert_file(self, json_path: str | Path) -> str:
        path = Path(json_path)
        with path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)

        if not isinstance(payload, dict):
            raise ValueError(
                f"ER JSON root must be a dict, got: {type(payload).__name__}"
            )

        return self.convert(payload)

    def convert(self, er_model: dict[str, Any]) -> str:
        entity_types = er_model.get("entity_types", [])
        relationship_types = er_model.get("relationship_types", [])

        if not isinstance(entity_types, list):
            raise ValueError("`entity_types` must be a list.")
        if not isinstance(relationship_types, list):
            raise ValueError("`relationship_types` must be a list.")

        alias_map = self._build_entity_alias_map(entity_types)
        lines: list[str] = ["erDiagram"]

        for entity in entity_types:
            lines.append("")
            lines.extend(self._build_entity_block(entity, alias_map))

        if relationship_types:
            lines.append("")
            for relationship in relationship_types:
                lines.append(self._build_relationship_line(relationship, alias_map))

        return "\n".join(lines)

    def write_mermaid_file(self, mermaid_code: str, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.write_text(mermaid_code, encoding="utf-8")
        return path

    def write_html_preview(
        self,
        mermaid_code: str,
        output_path: str | Path,
        *,
        title: str = "ER Diagram Preview",
    ) -> Path:
        path = Path(output_path)
        html = self._build_html(mermaid_code, title=title)
        path.write_text(html, encoding="utf-8")
        return path

    def export_image_with_mmdc(
        self,
        input_mermaid_path: str | Path,
        output_image_path: str | Path,
        *,
        background_color: str = "white",
        theme: str = "default",
    ) -> Path:
        mmdc = shutil.which("mmdc")
        if not mmdc:
            raise RuntimeError(
                "未检测到 mmdc（Mermaid CLI）。请先安装：npm install -g @mermaid-js/mermaid-cli"
            )

        input_path = Path(input_mermaid_path)
        output_path = Path(output_image_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            mmdc,
            "-i",
            str(input_path),
            "-o",
            str(output_path),
            "-t",
            theme,
            "-b",
            background_color,
        ]
        subprocess.run(cmd, check=True)
        return output_path

    def _build_entity_alias_map(
        self, entity_types: list[dict[str, Any]]
    ) -> dict[str, str]:
        alias_map: dict[str, str] = {}
        used_aliases: set[str] = set()

        for entity in entity_types:
            entity_name = entity.get("name")
            if not isinstance(entity_name, str) or not entity_name.strip():
                raise ValueError("Every entity must have a non-empty string `name`.")

            base_alias = self._normalize_identifier(entity_name).upper()
            alias = base_alias
            suffix = 2

            while alias in used_aliases:
                alias = f"{base_alias}_{suffix}"
                suffix += 1

            alias_map[entity_name] = alias
            used_aliases.add(alias)

        return alias_map

    def _build_entity_block(
        self, entity: dict[str, Any], alias_map: dict[str, str]
    ) -> list[str]:
        entity_name = entity["name"]
        entity_alias = alias_map[entity_name]
        raw_identifier_attrs = entity.get("identifier_attrs", [])
        attrs = entity.get("attrs", [])

        if not isinstance(raw_identifier_attrs, list):
            raise ValueError(
                f"`identifier_attrs` for entity `{entity_name}` must be a list."
            )
        if not isinstance(attrs, list):
            raise ValueError(f"`attrs` for entity `{entity_name}` must be a list.")

        identifier_attr_names = [
            attr_name.strip()
            for attr_name in raw_identifier_attrs
            if isinstance(attr_name, str) and attr_name.strip()
        ]
        identifier_attrs = set(identifier_attr_names)
        merged_attr_names = [self._extract_attr_name(attr, entity_name) for attr in attrs]
        existing_attr_names = set(merged_attr_names)

        for attr_name in identifier_attr_names:
            if attr_name not in existing_attr_names:
                merged_attr_names.append(attr_name)
                existing_attr_names.add(attr_name)

        lines = [f"    {entity_alias} {{"]

        for attr_name in merged_attr_names:
            attr_alias = self._normalize_identifier(attr_name)
            key_suffix = " PK" if attr_name in identifier_attrs else ""
            lines.append(f"        string {attr_alias}{key_suffix}")

        lines.append("    }")
        return lines

    def _extract_attr_name(self, attr: Any, entity_name: str) -> str:
        if isinstance(attr, str) and attr.strip():
            return attr.strip()
        if isinstance(attr, dict):
            attr_name = attr.get("name")
            if isinstance(attr_name, str) and attr_name.strip():
                return attr_name.strip()
        raise ValueError(
            f"Every attribute in entity `{entity_name}` must be a non-empty string or dict with `name`."
        )

    def _build_relationship_line(
        self, relationship: dict[str, Any], alias_map: dict[str, str]
    ) -> str:
        relationship_name = relationship.get("name")
        participants = relationship.get("participants", [])

        if not isinstance(relationship_name, str) or not relationship_name.strip():
            raise ValueError("Every relationship must have a non-empty string `name`.")
        if not isinstance(participants, list) or len(participants) != 2:
            raise ValueError(
                f"Relationship `{relationship_name}` must contain exactly two participants."
            )

        left_participant, right_participant = participants
        left_entity_name = left_participant.get("entity")
        right_entity_name = right_participant.get("entity")

        if left_entity_name not in alias_map:
            raise ValueError(
                f"Relationship `{relationship_name}` references unknown entity `{left_entity_name}`."
            )
        if right_entity_name not in alias_map:
            raise ValueError(
                f"Relationship `{relationship_name}` references unknown entity `{right_entity_name}`."
            )

        left_cardinality = self._to_mermaid_cardinality(
            left_participant.get("cardinality"), is_left=True
        )
        right_cardinality = self._to_mermaid_cardinality(
            right_participant.get("cardinality"), is_left=False
        )

        label = f" : {self._normalize_identifier(relationship_name)}" if self.show_relationship_labels else ""
        return (
            f"    {alias_map[left_entity_name]} {left_cardinality}--"
            f"{right_cardinality} {alias_map[right_entity_name]}{label}"
        )

    def _to_mermaid_cardinality(self, cardinality: Any, *, is_left: bool) -> str:
        if cardinality == "one":
            return "||"
        if cardinality == "many":
            return "}o" if is_left else "o{"
        if cardinality in {"unknown", None}:
            return "|o"

        raise ValueError(
            f"Unsupported cardinality `{cardinality}`. Expected `one`, `many`, or `unknown`."
        )

    def _normalize_identifier(self, value: str) -> str:
        normalized = self._NON_WORD_RE.sub("_", value.strip())
        normalized = normalized.strip("_")
        return normalized or "UNNAMED"

    def _build_html(self, mermaid_code: str, *, title: str) -> str:
        escaped = mermaid_code.replace("</", "<\\/")
        return textwrap.dedent(
            f"""\
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8" />
              <meta name="viewport" content="width=device-width, initial-scale=1" />
              <title>{title}</title>
              <style>
                body {{
                  margin: 0;
                  font-family: Arial, sans-serif;
                  background: #ffffff;
                }}
                .page {{
                  padding: 24px;
                }}
                .mermaid {{
                  overflow: auto;
                }}
              </style>
            </head>
            <body>
              <div class="page">
                <div class="mermaid">
            {escaped}
                </div>
              </div>

              <script type="module">
                import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
                mermaid.initialize({{
                  startOnLoad: true,
                  securityLevel: 'loose',
                  theme: 'default'
                }});
              </script>
            </body>
            </html>
            """
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="输入 ER JSON，输出 Mermaid 代码、HTML 预览，以及可选的 SVG/PNG 图片。"
    )
    parser.add_argument("input", help="ER JSON 文件路径")
    parser.add_argument(
        "--mmd",
        default="er_diagram.mmd",
        help="输出 Mermaid 文件路径，默认: er_diagram.mmd",
    )
    parser.add_argument(
        "--html",
        default="er_diagram.html",
        help="输出 HTML 预览文件路径，默认: er_diagram.html",
    )
    parser.add_argument(
        "--svg",
        default=None,
        help="可选：若本机已安装 mmdc，则导出 SVG 到该路径",
    )
    parser.add_argument(
        "--png",
        default=None,
        help="可选：若本机已安装 mmdc，则导出 PNG 到该路径",
    )
    parser.add_argument(
        "--show-relationship-labels",
        action="store_true",
        help="显示关系名称标注；默认不显示",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    converter = ERJsonToMermaidConverter(
        show_relationship_labels=args.show_relationship_labels
    )

    mermaid_code = converter.convert_file(args.input)
    mmd_path = converter.write_mermaid_file(mermaid_code, args.mmd)
    html_path = converter.write_html_preview(mermaid_code, args.html)

    print(f"[OK] Mermaid 文件已生成: {mmd_path}")
    print(f"[OK] HTML 预览已生成: {html_path}")

    if args.svg:
        svg_path = converter.export_image_with_mmdc(mmd_path, args.svg)
        print(f"[OK] SVG 图片已生成: {svg_path}")

    if args.png:
        png_path = converter.export_image_with_mmdc(mmd_path, args.png)
        print(f"[OK] PNG 图片已生成: {png_path}")


if __name__ == "__main__":
    main()
