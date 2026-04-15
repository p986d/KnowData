from __future__ import annotations

import argparse
import glob
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any


class ERJsonToMermaidConverter:
    """Convert ER-model JSON into Mermaid ER diagram code and preview files."""

    _NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)

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
        entity_types = er_model.get("entities", [])
        relation_types = er_model.get("relations", [])
        connection_types = er_model.get("connections", [])

        if not isinstance(entity_types, list):
            raise ValueError("`entities` must be a list.")
        if not isinstance(relation_types, list):
            raise ValueError("`relations` must be a list.")
        if not isinstance(connection_types, list):
            raise ValueError("`connections` must be a list.")

        used_aliases: set[str] = set()
        entity_alias_map = self._build_named_alias_map(
            entity_types,
            "entity_name",
            item_label="entity",
            used_aliases=used_aliases,
        )
        relation_alias_map = self._build_named_alias_map(
            relation_types,
            "relation_name",
            item_label="relation",
            used_aliases=used_aliases,
        )
        lines: list[str] = ["erDiagram"]

        for entity in entity_types:
            lines.append("")
            lines.extend(self._build_entity_block(entity, entity_alias_map))

        for relation in relation_types:
            lines.append("")
            lines.extend(
                self._build_association_block(
                    relation,
                    relation_alias_map,
                    name_field="relation_name",
                    item_label="relation",
                )
            )

        if relation_types or connection_types:
            lines.append("")
            for relation in relation_types:
                lines.extend(
                    self._build_association_lines(
                        relation,
                        entity_alias_map,
                        relation_alias_map,
                        name_field="relation_name",
                        item_label="relation",
                    )
                )
            for connection in connection_types:
                lines.extend(
                    self._build_connection_lines(
                        connection,
                        entity_alias_map,
                    )
                )

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

    def _build_named_alias_map(
        self,
        items: list[dict[str, Any]],
        name_field: str,
        *,
        item_label: str,
        used_aliases: set[str],
    ) -> dict[str, str]:
        alias_map: dict[str, str] = {}

        for item in items:
            item_name = item.get(name_field)
            if not isinstance(item_name, str) or not item_name.strip():
                raise ValueError(
                    f"Every {item_label} must have a non-empty string `{name_field}`."
                )

            base_alias = self._normalize_identifier(item_name)
            alias = base_alias
            suffix = 2

            while alias in used_aliases:
                alias = f"{base_alias}_{suffix}"
                suffix += 1

            alias_map[item_name] = alias
            used_aliases.add(alias)

        return alias_map

    def _build_entity_block(
        self, entity: dict[str, Any], alias_map: dict[str, str]
    ) -> list[str]:
        entity_name = entity["entity_name"]
        entity_alias = alias_map[entity_name]
        raw_identifier_attrs = entity.get("primary_key", [])
        attrs = entity.get("attributes", [])

        if not isinstance(raw_identifier_attrs, list):
            raise ValueError(
                f"`primary_key` for entity `{entity_name}` must be a list."
            )
        if not isinstance(attrs, list):
            raise ValueError(f"`attributes` for entity `{entity_name}` must be a list.")

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

    def _build_association_block(
        self,
        association: dict[str, Any],
        alias_map: dict[str, str],
        *,
        name_field: str,
        item_label: str,
    ) -> list[str]:
        association_name = association[name_field]
        association_alias = alias_map[association_name]
        participants = association.get("participants", [])
        attrs = association.get("attributes", [])

        if not isinstance(participants, list) or not participants:
            raise ValueError(
                f"{item_label.capitalize()} `{association_name}` must contain at least one participant."
            )
        if not isinstance(attrs, list):
            raise ValueError(
                f"`attributes` for {item_label} `{association_name}` must be a list."
            )

        merged_attrs = self._build_participant_anchor_attrs(
            participants, association_name, item_label
        )
        existing_attr_names = {attr_name for attr_name, _ in merged_attrs}

        for attr in attrs:
            attr_name = self._extract_attr_name(attr, association_name)
            if attr_name not in existing_attr_names:
                merged_attrs.append((attr_name, False))
                existing_attr_names.add(attr_name)

        lines = [f"    {association_alias} {{"]
        for attr_name, is_foreign_key in merged_attrs:
            attr_alias = self._normalize_identifier(attr_name)
            key_suffix = " FK" if is_foreign_key else ""
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

    def _build_participant_anchor_attrs(
        self,
        participants: list[dict[str, Any]],
        association_name: str,
        item_label: str,
    ) -> list[tuple[str, bool]]:
        attr_names: list[tuple[str, bool]] = []
        seen: set[str] = set()

        for index, participant in enumerate(participants, start=1):
            role_name = participant.get("role")
            role_label = (
                role_name.strip()
                if isinstance(role_name, str) and role_name.strip()
                else f"participant_{index}"
            )
            anchor_attributes = participant.get("anchor_attribute", [])
            if not isinstance(anchor_attributes, list):
                raise ValueError(
                    f"`anchor_attribute` for {item_label} `{association_name}` participant `{role_label}` must be a list."
                )

            role_alias = self._normalize_identifier(role_label)
            for anchor_attr in anchor_attributes:
                if not isinstance(anchor_attr, str) or not anchor_attr.strip():
                    raise ValueError(
                        f"Each `anchor_attribute` in {item_label} `{association_name}` participant `{role_label}` must be a non-empty string."
                    )
                merged_name = f"{role_alias}_{anchor_attr.strip()}"
                if merged_name not in seen:
                    attr_names.append((merged_name, True))
                    seen.add(merged_name)

        return attr_names

    def _build_association_lines(
        self,
        association: dict[str, Any],
        entity_alias_map: dict[str, str],
        association_alias_map: dict[str, str],
        *,
        name_field: str,
        item_label: str,
    ) -> list[str]:
        association_name = association.get(name_field)
        participants = association.get("participants", [])

        if not isinstance(association_name, str) or not association_name.strip():
            raise ValueError(
                f"Every {item_label} must have a non-empty string `{name_field}`."
            )
        if not isinstance(participants, list) or not participants:
            raise ValueError(
                f"{item_label.capitalize()} `{association_name}` must contain at least one participant."
            )

        association_alias = association_alias_map[association_name]
        lines: list[str] = []
        for index, participant in enumerate(participants, start=1):
            entity_name = participant.get("entity")
            if entity_name not in entity_alias_map:
                raise ValueError(
                    f"{item_label.capitalize()} `{association_name}` references unknown entity `{entity_name}`."
                )

            label = self._build_participant_label(
                participant,
                association_name=association_name,
                participant_index=index,
            )
            lines.append(
                f"    {entity_alias_map[entity_name]} ||--o{{ {association_alias}{label}"
            )

        return lines

    def _build_connection_lines(
        self,
        connection: dict[str, Any],
        entity_alias_map: dict[str, str],
    ) -> list[str]:
        connection_name = connection.get("connection_name")
        participants = connection.get("participants", [])

        if not isinstance(connection_name, str) or not connection_name.strip():
            raise ValueError(
                "Every connection must have a non-empty string `connection_name`."
            )
        if not isinstance(participants, list) or len(participants) != 2:
            raise ValueError(
                f"Connection `{connection_name}` must contain exactly two participants."
            )

        left_participant, right_participant = participants
        left_entity_name = left_participant.get("entity")
        right_entity_name = right_participant.get("entity")

        if left_entity_name not in entity_alias_map:
            raise ValueError(
                f"Connection `{connection_name}` references unknown entity `{left_entity_name}`."
            )
        if right_entity_name not in entity_alias_map:
            raise ValueError(
                f"Connection `{connection_name}` references unknown entity `{right_entity_name}`."
            )

        label = self._build_connection_label(
            left_participant,
            right_participant,
            connection_name=connection_name,
        )
        return [
            f"    {entity_alias_map[left_entity_name]} ||--|| {entity_alias_map[right_entity_name]}{label}"
        ]

    def _build_connection_label(
        self,
        left_participant: dict[str, Any],
        right_participant: dict[str, Any],
        *,
        connection_name: str,
    ) -> str:
        connection_alias = self._normalize_identifier(connection_name)
        return f" : {connection_alias}"

    def _build_participant_label(
        self,
        participant: dict[str, Any],
        *,
        association_name: str,
        participant_index: int,
    ) -> str:
        label_parts: list[str] = []

        role_name = participant.get("role")
        if isinstance(role_name, str) and role_name.strip():
            label_parts.append(self._normalize_identifier(role_name))
        else:
            label_parts.append(f"participant_{participant_index}")

        if self.show_relationship_labels:
            label_parts.append(self._normalize_identifier(association_name))

        return f" : {'__'.join(label_parts)}"

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
    parser.add_argument(
        "input",
        nargs="+",
        help="ER JSON 文件路径，或包含一组 nl2er_output.json 的目录路径/通配符",
    )
    parser.add_argument(
        "--mmd",
        default=None,
        help="单文件模式下的 Mermaid 输出路径；默认与输入同目录同名，扩展名为 .mmd",
    )
    parser.add_argument(
        "--html",
        default=None,
        help="单文件模式下的 HTML 输出路径；默认与输入同目录同名，扩展名为 .html",
    )
    parser.add_argument(
        "--svg",
        default=None,
        help="单文件模式下可选：若本机已安装 mmdc，则导出 SVG 到该路径",
    )
    parser.add_argument(
        "--png",
        default=None,
        help="单文件模式下可选：若本机已安装 mmdc，则导出 PNG 到该路径",
    )
    parser.add_argument(
        "--show-relationship-labels",
        action="store_true",
        help="显示关系名称标注；默认不显示",
    )
    return parser.parse_args()


def resolve_input_paths(raw_inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    def add_path(candidate: Path) -> None:
        resolved = candidate.resolve()
        if resolved not in seen:
            paths.append(candidate)
            seen.add(resolved)

    def expand_candidate(candidate: Path) -> None:
        if candidate.is_dir():
            matched = sorted(candidate.rglob("nl2er_output.json"))
            if not matched:
                raise FileNotFoundError(
                    f"目录中未找到 nl2er_output.json: {candidate}"
                )
            for file_path in matched:
                add_path(file_path)
            return
        if candidate.is_file():
            add_path(candidate)
            return
        raise FileNotFoundError(f"输入路径不存在: {candidate}")

    for raw_input in raw_inputs:
        matched_paths = [Path(match) for match in glob.glob(raw_input, recursive=True)]
        if matched_paths:
            for matched_path in sorted(matched_paths):
                expand_candidate(matched_path)
            continue
        expand_candidate(Path(raw_input))

    if not paths:
        raise FileNotFoundError("未找到任何可处理的 ER JSON 输入文件。")
    return paths


def default_output_path(input_path: Path, suffix: str) -> Path:
    return input_path.with_suffix(suffix)


def main() -> None:
    args = parse_args()
    converter = ERJsonToMermaidConverter(
        show_relationship_labels=args.show_relationship_labels
    )
    input_paths = resolve_input_paths(args.input)

    if len(input_paths) > 1 and any([args.mmd, args.html, args.svg, args.png]):
        raise ValueError("批量模式下不支持 --mmd/--html/--svg/--png，请使用默认同目录输出。")

    for input_path in input_paths:
        mermaid_code = converter.convert_file(input_path)
        mmd_path = converter.write_mermaid_file(
            mermaid_code,
            args.mmd if args.mmd else default_output_path(input_path, ".mmd"),
        )
        html_path = converter.write_html_preview(
            mermaid_code,
            args.html if args.html else default_output_path(input_path, ".html"),
        )

        print(f"[OK] 输入文件: {input_path}")
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
