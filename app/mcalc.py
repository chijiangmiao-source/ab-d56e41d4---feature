"""模态 μ-演算：词法、语法、静态校验与 Knaster–Tarski 固定点迭代。

文法（公式仅允许命题、!、&、|、<>、[]、μX.、νX. 与变量）::

    formula := or_expr
    or_expr := and_expr ('|' and_expr)*
    and_expr := unary ('&' unary)*
    unary := '!' unary | modal | app
    modal  := ('<' '>' | '[' ']') unary
    app    := ('mu' | 'nu') IDENT '.' formula | atom
    atom   := IDENT | '(' formula ')'

绑定关键字 ``mu`` / ``nu`` 分别写作符号 ``μ`` / ``ν``；为便于 HTTP 传输，
ASCII 形式 ``muX.`` / ``nuX.`` 同样接受，输出统一为 ``μ`` / ``ν``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------

_MU = "μ"
_NU = "ν"


@dataclass(frozen=True)
class Token:
    kind: str  # IDENT | MU | NU | DOT | AND | OR | NOT | DIAMOND | BOX | LPAREN | RPAREN
    text: str
    pos: int


class SyntaxError_(Exception):
    """公式或标识符不满足规范。"""


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() and ch not in (_MU, _NU)


def _is_ident_part(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        if ch == _MU:
            tokens.append(Token("MU", ch, i))
            i += 1
        elif ch == _NU:
            tokens.append(Token("NU", ch, i))
            i += 1
        elif (
            ch in ("m", "n")
            and (m := re.match(r"(mu|nu)(?=\s*[A-Za-z_][A-Za-z0-9_]*\s*\.)", src[i:]))
        ):
            tokens.append(Token("MU" if m.group(1) == "mu" else "NU",
                                m.group(1), i))
            i += 2
        elif ch == ".":
            tokens.append(Token("DOT", ch, i))
            i += 1
        elif ch == "&":
            tokens.append(Token("AND", ch, i))
            i += 1
        elif ch == "|":
            tokens.append(Token("OR", ch, i))
            i += 1
        elif ch == "!":
            tokens.append(Token("NOT", ch, i))
            i += 1
        elif ch == "<" and i + 1 < n and src[i + 1] == ">":
            tokens.append(Token("DIAMOND", "<>", i))
            i += 2
        elif ch == "[" and i + 1 < n and src[i + 1] == "]":
            tokens.append(Token("BOX", "[]", i))
            i += 2
        elif ch == "(":
            tokens.append(Token("LPAREN", ch, i))
            i += 1
        elif ch == ")":
            tokens.append(Token("RPAREN", ch, i))
            i += 1
        elif _is_ident_start(ch):
            j = i + 1
            while j < n and _is_ident_part(src[j]):
                j += 1
            tokens.append(Token("IDENT", src[i:j], i))
            i = j
        else:
            raise SyntaxError_(f"位置 {i} 处存在非法字符或语法残留: {ch!r}")
    return tokens


# ---------------------------------------------------------------------------
# 语法树
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Prop:
    name: str


@dataclass(frozen=True)
class Not:
    inner: "Formula"


@dataclass(frozen=True)
class And:
    left: "Formula"
    right: "Formula"


@dataclass(frozen=True)
class Or:
    left: "Formula"
    right: "Formula"


@dataclass(frozen=True)
class Diamond:
    inner: "Formula"


@dataclass(frozen=True)
class Box:
    inner: "Formula"


@dataclass(frozen=True)
class Mu:
    var: str
    inner: "Formula"


@dataclass(frozen=True)
class Nu:
    var: str
    inner: "Formula"


Formula = Union[Var, Prop, Not, And, Or, Diamond, Box, Mu, Nu]


class Parser:
    def __init__(self, tokens: list[Token], src: str) -> None:
        self.tokens = tokens
        self.src = src
        self.i = 0

    def _peek(self) -> Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self, kind: str) -> Token:
        tok = self._peek()
        if tok is None or tok.kind != kind:
            expected = {
                "IDENT": "标识符",
                "DOT": "'.'",
                "RPAREN": "')'",
            }.get(kind, kind)
            at = tok.pos if tok else len(self.src)
            raise SyntaxError_(f"位置 {at} 处缺少 {expected} 或公式不完整")
        self.i += 1
        return tok

    def parse(self) -> Formula:
        node = self.parse_or()
        if self._peek() is not None:
            tok = self._peek()
            raise SyntaxError_(f"位置 {tok.pos} 处存在语法残留: {tok.text!r}")
        return node

    def parse_or(self) -> Formula:
        node = self.parse_and()
        while self._peek() is not None and self._peek().kind == "OR":
            self.i += 1
            node = Or(node, self.parse_and())
        return node

    def parse_and(self) -> Formula:
        node = self.parse_unary()
        while self._peek() is not None and self._peek().kind == "AND":
            self.i += 1
            node = And(node, self.parse_unary())
        return node

    def parse_unary(self) -> Formula:
        tok = self._peek()
        if tok is None:
            raise SyntaxError_("公式不完整")
        if tok.kind == "NOT":
            self.i += 1
            return Not(self.parse_unary())
        if tok.kind in ("DIAMOND", "BOX"):
            self.i += 1
            inner = self.parse_unary()
            return Diamond(inner) if tok.kind == "DIAMOND" else Box(inner)
        if tok.kind in ("MU", "NU"):
            self.i += 1
            name = self._take("IDENT").text
            self._take("DOT")
            inner = self.parse_or()
            return Mu(name, inner) if tok.kind == "MU" else Nu(name, inner)
        return self.parse_atom()

    def parse_atom(self) -> Formula:
        tok = self._peek()
        if tok is None:
            raise SyntaxError_("公式不完整")
        if tok.kind == "IDENT":
            self.i += 1
            return Prop(tok.text)
        if tok.kind == "LPAREN":
            self.i += 1
            node = self.parse_or()
            self._take("RPAREN")
            return node
        raise SyntaxError_(f"位置 {tok.pos} 处出现意外符号: {tok.text!r}")


def parse_formula(src: str) -> Formula:
    if not src or not src.strip():
        raise SyntaxError_("公式为空")
    tokens = tokenize(src)
    return Parser(tokens, src).parse()


# ---------------------------------------------------------------------------
# 静态校验：命题、变量绑定（就近）、守卫性、重复绑定名
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    """公式未通过静态校验。"""


@dataclass
class Scope:
    """词法环境，记录当前可见的固定点绑定（最近者优先）。

    每条记录保存绑定处的模态嵌套深度与否定嵌套深度，用于精确判断
    变量引用是否受「绑定点与引用点之间」的模态算子守卫、否定奇偶是否单调。
    """

    bindings: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    bound_order: list[str] = field(default_factory=list)

    def child(self, var: str, kind: str, modal_depth: int, neg_depth: int) -> "Scope":
        if var in self.bindings:
            raise ValidationError(
                f"重复绑定名 {var!r}：变量在其作用域内被再次固定点绑定"
            )
        sub = Scope(dict(self.bindings), list(self.bound_order))
        sub.bindings[var] = (kind, modal_depth, neg_depth)
        sub.bound_order.append(var)
        return sub


def _resolve_vars(n: Formula, bound: tuple[str, ...]) -> Formula:
    """词法上命题与变量同为 IDENT：在绑定作用域内者解析为变量引用（就近）。"""
    if isinstance(n, Prop):
        return Var(n.name) if bound and n.name in bound else n
    if isinstance(n, Not):
        return Not(_resolve_vars(n.inner, bound))
    if isinstance(n, And):
        return And(_resolve_vars(n.left, bound), _resolve_vars(n.right, bound))
    if isinstance(n, Or):
        return Or(_resolve_vars(n.left, bound), _resolve_vars(n.right, bound))
    if isinstance(n, Diamond):
        return Diamond(_resolve_vars(n.inner, bound))
    if isinstance(n, Box):
        return Box(_resolve_vars(n.inner, bound))
    if isinstance(n, (Mu, Nu)):
        cls = Mu if isinstance(n, Mu) else Nu
        return cls(n.var, _resolve_vars(n.inner, bound + (n.var,)))
    return n


def validate(node: Formula, propositions: set[str]) -> Formula:
    """静态校验并返回变量已解析的语法树。

    检查项：命题均已声明；变量均有绑定；绑定名不重复（故引用者即最近绑定者）；
    每个绑定变量都受模态算子（``<>``/``[]``）守卫，且在偶数重否定下出现
    （保证固定点算子单调，Knaster–Tarski 迭代良定义）。
    """

    def check(n: Formula, scope: Scope, modal_depth: int, neg_depth: int) -> None:
        if isinstance(n, Var):
            if n.name not in scope.bindings:
                raise ValidationError(f"未绑定变量: {n.name!r}")
            _, bound_modal, bound_neg = scope.bindings[n.name]
            # 守卫模态/否定奇偶均以绑定点为基准，外层上下文不串味。
            if modal_depth <= bound_modal:
                raise ValidationError(
                    f"变量 {n.name!r} 未受其绑定点与引用点之间的模态算子（<> 或 []）守卫"
                )
            if (neg_depth - bound_neg) % 2 == 1:
                raise ValidationError(
                    f"变量 {n.name!r} 在其绑定作用域内奇数重否定下出现，固定点算子不单调"
                )
        elif isinstance(n, Prop):
            if n.name not in propositions:
                raise ValidationError(f"未定义的位置命题: {n.name!r}")
        elif isinstance(n, Not):
            check(n.inner, scope, modal_depth, neg_depth + 1)
        elif isinstance(n, (And, Or)):
            check(n.left, scope, modal_depth, neg_depth)
            check(n.right, scope, modal_depth, neg_depth)
        elif isinstance(n, (Diamond, Box)):
            check(n.inner, scope, modal_depth + 1, neg_depth)
        elif isinstance(n, (Mu, Nu)):
            sub = scope.child(
                n.var, "mu" if isinstance(n, Mu) else "nu", modal_depth, neg_depth
            )
            check(n.inner, sub, modal_depth, neg_depth)
        else:  # pragma: no cover - 穷尽性
            raise ValidationError("未知公式节点")

    resolved = _resolve_vars(node, ())
    check(resolved, Scope(), 0, 0)
    return resolved


# ---------------------------------------------------------------------------
# 语义：Knaster–Tarski 迭代
# ---------------------------------------------------------------------------


@dataclass
class FixpointTrace:
    """单个固定点绑定的一次完整迭代的证据。"""

    var: str
    kind: str  # 'mu' | 'nu'
    binder: str  # 规范文本，如 "νX."
    start: list[str]
    iterations: list[list[str]]  # 每次迭代后的集合（含初始态 index 0）
    stable: list[str]
    converged_at: int  # 首次与上一轮相等的迭代序号（stages[k]==stages[k-1]，k≥1）
    occurrence: int = 1  # 同一绑定在外层固定点各轮迭代中的第几次求值


@dataclass
class EvalResult:
    satisfied: list[str]  # 按位置标识排序
    initial_satisfied: bool
    traces: list[FixpointTrace]


def _pre(image_target: set[str], transitions: dict[str, list[str]]) -> set[str]:
    """前驱像：存在一条迁移进入目标集合的源位置。"""
    out: set[str] = set()
    for src, dests in transitions.items():
        if any(d in image_target for d in dests):
            out.add(src)
    return out


def evaluate(
    node: Formula,
    labels: dict[str, set[str]],
    transitions: dict[str, list[str]],
    order: list[str],
    initial: str,
) -> EvalResult:
    """在 Kripke 结构上求值；每个 μ 从 ∅、ν 从全集单调迭代至稳定。

    ``node`` 须为 :func:`validate` 返回的、变量已解析的语法树。
    环境 ``env`` 沿词法作用域传递；静态校验禁止重复绑定名，内层绑定
    仅存在于其迭代用的局部环境中，退出后不污染外层环境。
    """

    traces: list[FixpointTrace] = []
    occurrences: dict[int, int] = {}  # 绑定节点 id -> 该绑定在外层各轮中第几次求值

    def eval_(n: Formula, env: dict[str, frozenset[str]]) -> frozenset[str]:
        if isinstance(n, Var):
            return env[n.name]
        if isinstance(n, Prop):
            return frozenset(labels.get(n.name, frozenset()))
        if isinstance(n, Not):
            return frozenset(set(order) - set(eval_(n.inner, env)))
        if isinstance(n, And):
            return eval_(n.left, env) & eval_(n.right, env)
        if isinstance(n, Or):
            return eval_(n.left, env) | eval_(n.right, env)
        if isinstance(n, Diamond):
            return frozenset(_pre(set(eval_(n.inner, env)), transitions))
        if isinstance(n, Box):
            inner = set(eval_(n.inner, env))
            # []φ = 不存在后继使其 ¬φ；无后继（含悬空）位置空洞为真。
            return frozenset(
                s for s in order if all(d in inner for d in transitions.get(s, ()))
            )
        if isinstance(n, (Mu, Nu)):
            is_mu = isinstance(n, Mu)
            universe = frozenset(order)
            current = frozenset() if is_mu else universe
            binder = f"{'μ' if is_mu else 'ν'}{n.var}."
            seq: list[list[str]] = [sorted(current, key=order.index)]
            # 内层固定点在 local_env 中迭代；静态校验禁止重复绑定名，
            # 故各绑定变量互不遮蔽，退出子作用域后外层环境不受污染。
            local_env = dict(env)
            steps = 0
            while True:
                local_env[n.var] = current
                nxt = eval_(n.inner, local_env)
                steps += 1
                seq.append(sorted(nxt, key=order.index))
                if nxt == current:
                    break
                if is_mu:
                    if not (current <= nxt):  # pragma: no cover - 单调性恒成立
                        raise RuntimeError("μ 迭代非单调")
                else:
                    if not (nxt <= current):  # pragma: no cover - 单调性恒成立
                        raise RuntimeError("ν 迭代非单调")
                current = nxt
                if steps > len(order) + 1:  # pragma: no cover - 有限格必有稳定点
                    raise RuntimeError("固定点迭代未收敛")
            # 子作用域结束：移除本层绑定，恢复外层环境（无同名遮蔽，直接弹出）。
            local_env.pop(n.var, None)
            occ = occurrences.get(id(n), 0) + 1
            occurrences[id(n)] = occ
            traces.append(
                FixpointTrace(
                    var=n.var,
                    kind="mu" if is_mu else "nu",
                    binder=binder,
                    occurrence=occ,
                    start=seq[0],
                    iterations=seq,
                    stable=seq[-1],
                    converged_at=steps,
                )
            )
            return current
        raise RuntimeError("未知公式节点")  # pragma: no cover

    sat = eval_(node, {})
    sat_list = sorted(sat, key=order.index)
    return EvalResult(sat_list, initial in sat, traces)


def pretty(node: Formula) -> str:
    """规范化输出公式。"""
    if isinstance(node, Prop) or isinstance(node, Var):
        return node.name
    if isinstance(node, Not):
        return f"!{pretty(node.inner)}"
    if isinstance(node, And):
        return f"({pretty(node.left)} & {pretty(node.right)})"
    if isinstance(node, Or):
        return f"({pretty(node.left)} | {pretty(node.right)})"
    if isinstance(node, Diamond):
        return f"<>{pretty(node.inner)}"
    if isinstance(node, Box):
        return f"[]{pretty(node.inner)}"
    if isinstance(node, (Mu, Nu)):
        sym = "μ" if isinstance(node, Mu) else "ν"
        return f"{sym}{node.var}.{pretty(node.inner)}"
    raise RuntimeError  # pragma: no cover
