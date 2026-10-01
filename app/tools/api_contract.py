import ast
from typing import Any

from app.tools.workspace import WorkspaceTool


class BackendAPIContractCollector:
    def __init__(self, workspace: WorkspaceTool) -> None:
        self.workspace = workspace

    def collect(self) -> dict[str, Any]:
        routes = []
        schemas = {}
        for path in self.workspace.list_files():
            if not path.startswith("backend/app/") or not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(self.workspace.read_file(path))
            except (OSError, UnicodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for decorator in node.decorator_list:
                        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                            continue
                        method = decorator.func.attr.upper()
                        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                            continue
                        endpoint = ""
                        if decorator.args and isinstance(decorator.args[0], ast.Constant):
                            endpoint = str(decorator.args[0].value)
                        routes.append({"method": method, "path": endpoint, "handler": node.name, "source": path})
                if isinstance(node, ast.ClassDef) and any((isinstance(base, ast.Name) and base.id == "BaseModel") or (isinstance(base, ast.Attribute) and base.attr == "BaseModel") for base in node.bases):
                    schemas[node.name] = [child.target.id for child in node.body if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)]
        return {"routes": routes, "schemas": schemas}
