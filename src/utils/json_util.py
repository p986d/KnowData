import json
import re


_JSON_FENCE_RE = re.compile(
    r"```json\s*(.*?)\s*```",
    flags=re.DOTALL | re.IGNORECASE,
)

def _fix_llm_json_encoding(text: str) -> str:
    """
    修复常见 LLM JSON 编码/转义问题：
    1) 非法 JSON 转义: \\xNN
    2) mojibake: â\x80\x99 -> ’
    """
    if not text:
        return text

    t = text

    def _hex_to_char(m: re.Match[str]) -> str:
        return chr(int(m.group(1), 16))

    # 1. 把字面量 "\\xNN" 转成真实字符，避免 json.loads 报 invalid escape
    if "\\x" in t:
        t = re.sub(r"\\x([0-9a-fA-F]{2})", _hex_to_char, t)

    # 2. 修复常见 mojibake
    def _has_c1_controls(s: str) -> bool:
        return any(0x80 <= ord(ch) <= 0x9F for ch in s)

    if ("â" in t) or _has_c1_controls(t):
        try:
            t = t.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    return t


def json_extract(raw_text: str) -> str:
    """
    提取最后一个 ```json ... ``` 代码块。
    如果不存在，则返回整个文本的 strip 结果。
    """
    matches = _JSON_FENCE_RE.findall(raw_text)
    candidate = matches[-1] if matches else raw_text
    return _fix_llm_json_encoding(candidate.strip())

def json_parse(raw_text: str) -> dict:
    """
    Parse JSON from the last fenced ```json ... ``` block when present.
    Otherwise, fall back to parsing the stripped raw text as bare JSON.
    """
    extracted = json_extract(raw_text)

    try:
        data = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to decode extracted JSON payload:\n#####\n{extracted}\n#####"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(f"Parsed JSON is not a dict, got: {type(data).__name__}")

    return data


def json_check(raw_text: str) -> bool:
    """
    Return True when the extracted payload is valid JSON.
    Supports both fenced ```json ... ``` output and bare JSON output.
    """
    extracted = json_extract(raw_text)
    if not extracted:
        return False

    try:
        json.loads(extracted)
        return True
    except json.JSONDecodeError:
        print(f"Error decoding JSON: \n ##### \n {extracted} \n #####")
        return False
