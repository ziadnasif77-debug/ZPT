"""Project Mapper — builds a complete semantic map of the workspace.

Uses AST to parse every .py file and build a structured map of classes,
methods, attributes, functions, and imports. Then compares what tests
EXPECT vs what code PROVIDES to find gaps.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ClassMap:
    name: str
    methods: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    init_params: list[str] = field(default_factory=list)
    line: int = 0


@dataclass
class FileMap:
    path: str
    classes: dict[str, ClassMap] = field(default_factory=dict)
    functions: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    imports_from: dict[str, list[str]] = field(default_factory=dict)
    top_level_names: list[str] = field(default_factory=list)


@dataclass
class Gap:
    gap_type: str
    expected_by: str
    should_be_in: str
    class_name: str = ""
    element_name: str = ""
    call_args: list[str] = field(default_factory=list)
    call_kwargs: list[str] = field(default_factory=list)
    usage_line: int = 0


@dataclass
class ProjectMap:
    files: dict[str, FileMap] = field(default_factory=dict)
    gaps: list[Gap] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)


class ProjectMapper:
    def map_workspace(self, workspace: Path) -> ProjectMap:
        pmap = ProjectMap()

        if not workspace.is_dir():
            return pmap

        for py_file in sorted(workspace.glob("*.py")):
            try:
                source = py_file.read_text(encoding="utf-8")
                fmap = self._map_file(py_file.name, source)
                pmap.files[py_file.name] = fmap
            except Exception:
                pmap.files[py_file.name] = FileMap(path=py_file.name)

        self._build_dependencies(pmap)
        pmap.gaps = self.find_gaps(pmap)

        return pmap

    def _map_file(self, filename: str, source: str) -> FileMap:
        fmap = FileMap(path=filename)

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return fmap

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                cmap = self._map_class(node)
                fmap.classes[node.name] = cmap
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fmap.functions.append(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    fmap.imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [alias.name for alias in node.names]
                fmap.imports_from[node.module] = names
                fmap.imports.append(node.module)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        fmap.top_level_names.append(target.id)

        return fmap

    def _map_class(self, class_node: ast.ClassDef) -> ClassMap:
        cmap = ClassMap(name=class_node.name, line=class_node.lineno)

        for base in class_node.bases:
            if isinstance(base, ast.Name):
                cmap.bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                cmap.bases.append(ast.dump(base))

        for node in class_node.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cmap.methods.append(node.name)
                if node.name == "__init__":
                    cmap.init_params = [
                        a.arg for a in node.args.args if a.arg != "self"
                    ]
                    for child in ast.walk(node):
                        if (
                            isinstance(child, ast.Attribute)
                            and isinstance(child.value, ast.Name)
                            and child.value.id == "self"
                            and isinstance(child.ctx, ast.Store)
                        ):
                            cmap.attributes.append(child.attr)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        cmap.attributes.append(target.id)

        return cmap

    def _build_dependencies(self, pmap: ProjectMap):
        local_modules = {f.replace(".py", "") for f in pmap.files}

        for filename, fmap in pmap.files.items():
            deps: list[str] = []
            for imp in fmap.imports:
                mod = imp.split(".")[0]
                if mod in local_modules:
                    deps.append(mod + ".py")
            for mod in fmap.imports_from:
                mod_base = mod.split(".")[0]
                if mod_base in local_modules:
                    deps.append(mod_base + ".py")
            pmap.dependencies[filename] = deps

    def find_gaps(self, pmap: ProjectMap) -> list[Gap]:
        gaps: list[Gap] = []

        code_classes: dict[str, tuple[ClassMap, str]] = {}
        for filename, fmap in pmap.files.items():
            if filename.startswith("test") or filename == "test_runner.py":
                continue
            for cname, cmap in fmap.classes.items():
                code_classes[cname] = (cmap, filename)

        all_code_functions: dict[str, str] = {}
        for filename, fmap in pmap.files.items():
            if filename.startswith("test") or filename == "test_runner.py":
                continue
            for func in fmap.functions:
                all_code_functions[func] = filename

        for filename, fmap in pmap.files.items():
            if not filename.startswith("test") and filename != "test_runner.py":
                continue

            test_source_path = None
            for f in pmap.files:
                if f == filename:
                    test_source_path = f
                    break

            if not test_source_path:
                continue

            method_calls = self._extract_method_calls(pmap, filename)
            for class_name, method_name, args, kwargs, line in method_calls:
                if class_name in code_classes:
                    cmap, source_file = code_classes[class_name]
                    if method_name not in cmap.methods:
                        gaps.append(Gap(
                            gap_type="missing_method",
                            expected_by=f"{filename} line {line}",
                            should_be_in=source_file,
                            class_name=class_name,
                            element_name=method_name,
                            call_args=args,
                            call_kwargs=kwargs,
                            usage_line=line,
                        ))
                elif class_name:
                    gaps.append(Gap(
                        gap_type="missing_class",
                        expected_by=f"{filename} line {line}",
                        should_be_in="",
                        class_name=class_name,
                        element_name=class_name,
                        usage_line=line,
                    ))

            imported_names = self._extract_test_imports(pmap, filename)
            for mod_name, imported_name, line in imported_names:
                source_file = mod_name + ".py"
                if source_file in pmap.files:
                    sfmap = pmap.files[source_file]
                    all_names = (
                        set(sfmap.classes.keys())
                        | set(sfmap.functions)
                        | set(sfmap.top_level_names)
                    )
                    if imported_name not in all_names:
                        gaps.append(Gap(
                            gap_type="missing_export",
                            expected_by=f"{filename} line {line}",
                            should_be_in=source_file,
                            element_name=imported_name,
                            usage_line=line,
                        ))

        seen = set()
        unique_gaps = []
        for g in gaps:
            key = (g.gap_type, g.should_be_in, g.class_name, g.element_name)
            if key not in seen:
                seen.add(key)
                unique_gaps.append(g)

        return unique_gaps

    def _extract_method_calls(
        self, pmap: ProjectMap, test_filename: str
    ) -> list[tuple[str, str, list[str], list[str], int]]:
        fmap = pmap.files.get(test_filename)
        if not fmap:
            return []

        results: list[tuple[str, str, list[str], list[str], int]] = []

        try:
            source = (Path(".") / test_filename).read_text(encoding="utf-8")
        except Exception:
            return results

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return results

        var_types: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                        if isinstance(node.value.func, ast.Name):
                            var_types[target.id] = node.value.func.id

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                method = node.func.attr
                if isinstance(node.func.value, ast.Name):
                    var_name = node.func.value.id
                    class_name = var_types.get(var_name, "")
                    if class_name:
                        args = [ast.dump(a) for a in node.args]
                        kwargs = [kw.arg for kw in node.keywords if kw.arg]
                        results.append((class_name, method, args, kwargs, node.lineno))

        return results

    def _extract_test_imports(
        self, pmap: ProjectMap, test_filename: str
    ) -> list[tuple[str, str, int]]:
        fmap = pmap.files.get(test_filename)
        if not fmap:
            return []

        results: list[tuple[str, str, int]] = []
        local_modules = {f.replace(".py", "") for f in pmap.files if not f.startswith("test")}

        try:
            source = (Path(".") / test_filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except Exception:
            return results

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod_base = node.module.split(".")[0]
                if mod_base in local_modules:
                    for alias in node.names:
                        results.append((mod_base, alias.name, node.lineno))

        return results
