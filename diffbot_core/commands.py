"""声明式指令系统：规格定义、统一解析、权限等级、帮助生成。

修复现状问题：
- 裸 raw_message.split() 无法处理引号/多空格/含空格标题（统一走 shlex）
- "@xxx" 无条件当站点引用，导致标题含 @ 被吞（改为命中已知站点才算引用）
- 帮助文本手工维护、与实现漂移（改为由注册表生成）
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Sequence


class Level(IntEnum):
    """指令权限等级。"""

    USER = 0
    GROUP_ADMIN = 1
    SUPER_ADMIN = 2

    @property
    def label(self) -> str:
        return {
            Level.USER: "所有人",
            Level.GROUP_ADMIN: "群管理员",
            Level.SUPER_ADMIN: "超级管理员",
        }[self]


class CommandError(Exception):
    """业务错误：直接把 user_message 回给用户。"""

    def __init__(self, message: str, *, usage: str | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.usage = usage


@dataclass(frozen=True)
class ArgSpec:
    """位置参数。greedy=True 时吸收剩余全部 token（用于含空格标题）。"""

    name: str
    required: bool = True
    greedy: bool = False
    type: Callable[[str], Any] = str
    default: Any = None


@dataclass(frozen=True)
class FlagSpec:
    """--flag 形式的关键字参数。type=bool 时作为开关（无需取值）。"""

    name: str
    type: Callable[[str], Any] = str
    required: bool = False
    default: Any = None
    help: str = ""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    handler: Callable[[Any, "ParsedArgs"], Awaitable[None]] = None  # type: ignore[assignment]
    level: Level = Level.USER
    args: tuple[ArgSpec, ...] = ()
    flags: tuple[FlagSpec, ...] = ()
    usage: str = ""
    examples: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    group_scope: bool = True
    private_scope: bool = False
    allow_extra: bool = False
    needs_site: bool = False


@dataclass
class ParsedArgs:
    raw: str
    tokens: list[str]
    positionals: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    site_query: str | None = None

    def get(self, name: str, default: Any = None) -> Any:
        return self.positionals.get(name, default)

    def flag(self, name: str, default: Any = None) -> Any:
        return self.flags.get(name, default)


_FULLWIDTH = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "\u3000": " "})


def tokenize(raw: str) -> list[str]:
    """把原始消息切成 token（支持引号、多空格、全角引号）。"""
    text = (raw or "").translate(_FULLWIDTH).strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError as exc:
        raise CommandError(f"参数格式错误（引号不匹配）：{exc}") from exc


def parse_args(
    raw: str,
    spec: CommandSpec,
    *,
    site_lookup: Callable[[str], Any] | None = None,
) -> ParsedArgs:
    """解析指令参数。

    规则：
    - 第 1 个 token 是指令名（丢弃）
    - --name value 绑定到 FlagSpec；未知 flag 报错
    - @xxx 仅在 site_lookup(xxx) 命中时视为站点引用，否则当普通文本
    - 位置参数按 ArgSpec 顺序绑定；末位 greedy 参数吸收剩余 token
    """
    tokens = tokenize(raw)
    body = tokens[1:] if tokens else []
    flag_specs = {f.name: f for f in spec.flags}
    rest: list[str] = []
    flags: dict[str, Any] = {}
    site_query: str | None = None

    i = 0
    while i < len(body):
        tok = body[i]
        if tok.startswith("--") and len(tok) > 2:
            name = tok[2:]
            fs = flag_specs.get(name)
            if fs is None:
                raise CommandError(f"未知参数 --{name}", usage=spec.usage)
            if fs.type is bool:
                flags[name] = True
                i += 1
                continue
            if i + 1 >= len(body):
                raise CommandError(f"参数 --{name} 缺少取值", usage=spec.usage)
            raw_val = body[i + 1]
            try:
                flags[name] = fs.type(raw_val)
            except (TypeError, ValueError) as exc:
                raise CommandError(
                    f"参数 --{name} 取值无效：{raw_val}", usage=spec.usage
                ) from exc
            i += 2
            continue
        if tok.startswith("@") and len(tok) > 1:
            cand = tok[1:]
            if site_lookup is not None and site_lookup(cand) is not None:
                site_query = cand
                i += 1
                continue
        rest.append(tok)
        i += 1

    positionals: dict[str, Any] = {}
    cursor = 0
    for arg in spec.args:
        if arg.greedy:
            value = " ".join(rest[cursor:]).strip()
            cursor = len(rest)
            if not value:
                if arg.required:
                    raise CommandError(f"缺少参数：{arg.name}", usage=spec.usage)
                positionals[arg.name] = arg.default
            else:
                positionals[arg.name] = value
            continue
        if cursor < len(rest):
            raw_val = rest[cursor]
            cursor += 1
            try:
                positionals[arg.name] = arg.type(raw_val)
            except (TypeError, ValueError) as exc:
                raise CommandError(
                    f"参数 {arg.name} 格式错误：{raw_val}", usage=spec.usage
                ) from exc
        else:
            if arg.required:
                raise CommandError(f"缺少参数：{arg.name}", usage=spec.usage)
            positionals[arg.name] = arg.default

    extra = rest[cursor:]
    if extra and not spec.allow_extra:
        raise CommandError(f"参数过多：{' '.join(extra)}", usage=spec.usage)

    for fs in spec.flags:
        if fs.name not in flags:
            if fs.required:
                raise CommandError(f"缺少参数 --{fs.name}", usage=spec.usage)
            flags[fs.name] = fs.default

    return ParsedArgs(
        raw=raw, tokens=tokens, positionals=positionals, flags=flags,
        site_query=site_query,
    )


def usage_line(spec: CommandSpec, prefix: str = "/") -> str:
    if spec.usage:
        return f"{prefix}{spec.name} {spec.usage}"
    parts = [f"{prefix}{spec.name}"]
    for arg in spec.args:
        token = f"<{arg.name}>"
        parts.append(token if arg.required else f"[{token}]")
    for fs in spec.flags:
        token = f"--{fs.name}"
        if fs.type is not bool:
            token += " <值>"
        parts.append(f"[{token}]")
    return " ".join(parts)


def render_help(
    specs: Sequence[CommandSpec],
    level: Level = Level.USER,
    *,
    name: str | None = None,
    prefix: str = "/",
    subs_map: dict[str, Sequence[CommandSpec]] | None = None,
) -> str:
    """按权限裁剪生成帮助。name 给定时输出单条指令详情。"""
    visible = [s for s in specs if s.level <= level]
    if name:
        target = None
        for s in visible:
            if s.name == name or name in s.aliases:
                target = s
                break
        if target is None:
            return f"未找到指令 {prefix}{name}。用 {prefix}help 查看全部指令。"
        lines = [
            f"📖 {prefix}{target.name} — {target.summary}",
            f"权限：{target.level.label}",
            f"用法：{usage_line(target, prefix)}",
        ]
        if target.args:
            lines.append("参数：")
            for arg in target.args:
                mark = "" if arg.required else "（可选）"
                kind = "剩余全部文本" if arg.greedy else "单个词"
                lines.append(f"  • {arg.name}{mark}：{kind}")
        if target.flags:
            lines.append("选项：")
            for fs in target.flags:
                lines.append(f"  • --{fs.name}：{fs.help or '取值'}")
        if target.examples:
            lines.append("示例：")
            lines.extend(f"  {prefix}{target.name} {ex}" for ex in target.examples)
        return "\n".join(lines)

    lines = ["📖 DiffBot 指令"]
    for s in sorted(visible, key=lambda x: (x.level, x.name)):
        mark = "" if s.level == Level.USER else f"（{s.level.label}）"
        subs = (subs_map or {}).get(s.name)
        if subs:
            names = "|".join(sub.name for sub in subs)
            lines.append(f"{prefix}{s.name} {names}{mark} — {s.summary}")
        else:
            lines.append(f"{usage_line(s, prefix)}{mark} — {s.summary}")
    lines.append(f"用 {prefix}help <指令> 查看单条指令详情。")
    return "\n".join(lines)


def find_spec(specs: Sequence[CommandSpec], token: str) -> CommandSpec | None:
    """按名称或别名查找指令。"""
    for s in specs:
        if token == s.name or token in s.aliases:
            return s
    return None
