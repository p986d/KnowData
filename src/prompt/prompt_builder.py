from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

@dataclass(slots=True)
class PromptTemplateSpec:
    """
    描述一个完整 prompt 模板（通常对应 agent 的一个 step）
    """
    name: str
    template_name: str
    required_vars: set[str] = field(default_factory=set)
    default_vars: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(slots=True)
class PromptPartSpec:
    """
    描述一个可复用 part（可理解为 prompt 片段）
    """
    name: str
    template_name: str
    required_vars: set[str] = field(default_factory=set)
    default_vars: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(slots=True)
class PromptBuildResult:
    """
    返回构建结果，方便调试和日志记录
    """
    name: str
    final_text: str
    resolved_vars: dict[str, Any]
    rendered_parts: dict[str, str]


class PromptBuilder:
    """
    统一管理：
    1. step 级完整模板
    2. 可复用 parts
    3. shared vars + runtime vars 合并
    4. 最终 prompt / messages 输出
    """

    def __init__(
        self,
        template_dir: str | Path,
        *,
        strict_undefined: bool = True,
    ) -> None:
        self.template_dir = Path(template_dir)

        undefined_cls = StrictUndefined if strict_undefined else None

        env_kwargs: dict[str, Any] = {
            "loader": FileSystemLoader(str(self.template_dir)),
            "trim_blocks": True,
            "lstrip_blocks": True,
        }
        if undefined_cls is not None:
            env_kwargs["undefined"] = undefined_cls

        self.env = Environment(**env_kwargs)

        self._register_filters()
        self._register_globals()

        self.template_specs: dict[str, PromptTemplateSpec] = {}
        self.part_specs: dict[str, PromptPartSpec] = {}
        self.shared_vars: dict[str, Any] = {}

    # -----------------------------
    # public api
    # -----------------------------
    def set_shared_vars(self, **kwargs: Any) -> None:
        self.shared_vars.update(kwargs)

    def clear_shared_vars(self) -> None:
        self.shared_vars.clear()

    def register_template(
        self,
        *,
        name: str,
        template_name: str,
        required_vars: Optional[Sequence[str]] = None,
        default_vars: Optional[Mapping[str, Any]] = None,
        description: str = "",
    ) -> None:
        self._ensure_template_exists(template_name)
        self.template_specs[name] = PromptTemplateSpec(
            name=name,
            template_name=template_name,
            required_vars=set(required_vars or []),
            default_vars=dict(default_vars or {}),
            description=description,
        )

    def register_part(
        self,
        *,
        name: str,
        template_name: str,
        required_vars: Optional[Sequence[str]] = None,
        default_vars: Optional[Mapping[str, Any]] = None,
        description: str = "",
    ) -> None:
        self._ensure_template_exists(template_name)
        self.part_specs[name] = PromptPartSpec(
            name=name,
            template_name=template_name,
            required_vars=set(required_vars or []),
            default_vars=dict(default_vars or {}),
            description=description,
        )

    def render_part(
        self,
        part_name: str,
        *,
        vars: Optional[Mapping[str, Any]] = None,
        extra_vars: Optional[Mapping[str, Any]] = None,
    ) -> str:
        part_spec = self._get_part_spec(part_name)

        merged = self._merge_vars(
            self.shared_vars,
            part_spec.default_vars,
            vars or {},
            extra_vars or {},
        )
        self._check_required_vars(part_name, merged, part_spec.required_vars)

        template = self.env.get_template(part_spec.template_name)
        return template.render(**merged).strip()

    def build(
        self,
        template_name: str,
        *,
        vars: Optional[Mapping[str, Any]] = None,
        part_bindings: Optional[Mapping[str, str | Sequence[str]]] = None,
        extra_vars: Optional[Mapping[str, Any]] = None,
    ) -> PromptBuildResult:
        """
        template_name: 已注册的完整模板名
        vars: 本次 step 的变量
        part_bindings:
            将模板中的某些变量位绑定为 part
            例如:
            {
                "role_part": "role.er_expert",
                "rules_part": ["rules.json_output", "rules.no_hallucination"]
            }
        extra_vars:
            最后覆盖层，优先级最高
        """
        spec = self._get_template_spec(template_name)

        rendered_parts = self._render_bound_parts(
            part_bindings=part_bindings or {},
            base_vars=vars or {},
            extra_vars=extra_vars or {},
        )

        merged = self._merge_vars(
            self.shared_vars,
            spec.default_vars,
            vars or {},
            rendered_parts,
            extra_vars or {},
        )

        self._check_required_vars(spec.name, merged, spec.required_vars)

        template = self.env.get_template(spec.template_name)
        final_text = template.render(**merged).strip()

        return PromptBuildResult(
            name=template_name,
            final_text=final_text,
            resolved_vars=merged,
            rendered_parts=rendered_parts,
        )

    def build_text(
        self,
        template_name: str,
        *,
        vars: Optional[Mapping[str, Any]] = None,
        part_bindings: Optional[Mapping[str, str | Sequence[str]]] = None,
        extra_vars: Optional[Mapping[str, Any]] = None,
    ) -> str:
        return self.build(
            template_name,
            vars=vars,
            part_bindings=part_bindings,
            extra_vars=extra_vars,
        ).final_text

    def build_messages(
        self,
        template_name: str,
        *,
        vars: Optional[Mapping[str, Any]] = None,
        part_bindings: Optional[Mapping[str, str | Sequence[str]]] = None,
        extra_vars: Optional[Mapping[str, Any]] = None,
        system_prompt: Optional[str] = None,
    ) -> list[dict[str, str]]:
        """
        适合对接 ChatOpenAI / OpenAI Responses / LangChain messages
        """
        prompt_text = self.build_text(
            template_name,
            vars=vars,
            part_bindings=part_bindings,
            extra_vars=extra_vars,
        )

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt_text})
        return messages

    # -----------------------------
    # internals
    # -----------------------------
    def _register_filters(self) -> None:
        self.env.filters["tojson_pretty"] = self._tojson_pretty
        self.env.filters["strip"] = lambda s: s.strip() if isinstance(s, str) else s
        self.env.filters["join_nonempty"] = self._join_nonempty

    def _register_globals(self) -> None:
        self.env.globals["section"] = self._section
        self.env.globals["bullet_list"] = self._bullet_list

    def _ensure_template_exists(self, template_name: str) -> None:
        try:
            self.env.get_template(template_name)
        except TemplateNotFound as exc:
            raise FileNotFoundError(
                f"Template not found: {template_name!r} under {self.template_dir}"
            ) from exc

    def _get_template_spec(self, name: str) -> PromptTemplateSpec:
        if name not in self.template_specs:
            raise KeyError(f"Prompt template spec not registered: {name}")
        return self.template_specs[name]

    def _get_part_spec(self, name: str) -> PromptPartSpec:
        if name not in self.part_specs:
            raise KeyError(f"Prompt part spec not registered: {name}")
        return self.part_specs[name]

    @staticmethod
    def _merge_vars(*layers: Mapping[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for layer in layers:
            merged.update(layer)
        return merged

    @staticmethod
    def _check_required_vars(
        spec_name: str,
        vars: Mapping[str, Any],
        required_vars: set[str],
    ) -> None:
        missing = [k for k in required_vars if k not in vars or vars[k] is None]
        if missing:
            raise ValueError(
                f"Missing required vars for {spec_name}: {missing}"
            )

    def _render_bound_parts(
        self,
        *,
        part_bindings: Mapping[str, str | Sequence[str]],
        base_vars: Mapping[str, Any],
        extra_vars: Mapping[str, Any],
    ) -> dict[str, str]:
        rendered: dict[str, str] = {}

        for bind_name, part_ref in part_bindings.items():
            if isinstance(part_ref, str):
                rendered[bind_name] = self.render_part(
                    part_ref,
                    vars=base_vars,
                    extra_vars=extra_vars,
                )
            else:
                texts: list[str] = []
                for one_part in part_ref:
                    text = self.render_part(
                        one_part,
                        vars=base_vars,
                        extra_vars=extra_vars,
                    ).strip()
                    if text:
                        texts.append(text)
                rendered[bind_name] = "\n\n".join(texts).strip()

        return rendered

    @staticmethod
    def _tojson_pretty(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)

    @staticmethod
    def _join_nonempty(values: Sequence[Any], sep: str = "\n") -> str:
        return sep.join(str(v) for v in values if v is not None and str(v).strip())

    @staticmethod
    def _section(title: str, body: Any) -> str:
        if body is None:
            return ""
        body_str = str(body).strip()
        if not body_str:
            return ""
        return f"## {title}\n{body_str}"

    @staticmethod
    def _bullet_list(items: Sequence[Any]) -> str:
        cleaned = [str(x).strip() for x in items if x is not None and str(x).strip()]
        return "\n".join(f"- {x}" for x in cleaned)