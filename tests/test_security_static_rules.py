from __future__ import annotations

import ast
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent

DIRECT_GET_ROUTE_MODULES = [
    ROOT / "src/imghost/web/admin_api.py",
    ROOT / "src/imghost/web/health.py",
    ROOT / "src/imghost/web/media.py",
    ROOT / "src/imghost/web/metrics.py",
    ROOT / "src/imghost/web/public_api.py",
    ROOT / "src/imghost/web/user_api.py",
]

TRUST_BOUNDARY_ROUTE_MODULES = [
    ROOT / "src/imghost/web/admin_api.py",
    ROOT / "src/imghost/web/auth.py",
    ROOT / "src/imghost/web/auth_context.py",
    ROOT / "src/imghost/web/media.py",
    ROOT / "src/imghost/web/oauth.py",
    ROOT / "src/imghost/web/pages.py",
    ROOT / "src/imghost/web/public_api.py",
    ROOT / "src/imghost/web/user_api.py",
]

MUTATING_CALL_RE = re.compile(r"\.(create_|update_|delete_|issue_|consume_|touch_)[a-zA-Z0-9_]*\(")


def _module_ast(path: Path) -> tuple[str, ast.AST]:
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _is_router_get(decorator: ast.expr) -> bool:
    return isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "get"


def test_direct_api_get_handlers_do_not_call_mutating_helpers() -> None:
    offenders: list[str] = []

    for path in DIRECT_GET_ROUTE_MODULES:
        source, tree = _module_ast(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_router_get(decorator) for decorator in node.decorator_list):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if MUTATING_CALL_RE.search(segment):
                offenders.append(f"{path.relative_to(ROOT)}:{node.name}")

    assert offenders == []


def test_trust_boundary_route_modules_do_not_use_broad_exception_handlers() -> None:
    offenders: list[str] = []

    for path in TRUST_BOUNDARY_ROUTE_MODULES:
        _, tree = _module_ast(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                continue
            if isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert offenders == []
