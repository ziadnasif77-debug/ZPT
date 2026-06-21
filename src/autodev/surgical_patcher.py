"""Surgical Patcher — makes MINIMAL targeted changes to fix gaps.

Never rewrites whole files. One fix = one gap = one patch.
Uses AST-aware insertion to add methods, classes, or imports
at the exact right location while preserving all surrounding code.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from autodev.project_mapper import ClassMap, Gap, ProjectMap


@dataclass
class Patch:
    patch_type: str
    target_file: str
    description: str
    insert_after_line: int = 0
    code_to_insert: str = ""
    replace_line_start: int = 0
    replace_line_end: int = 0
    replacement_code: str = ""


@dataclass
class PatchResult:
    success: bool
    file_path: str
    description: str
    error: str = ""


class SurgicalPatcher:
    def plan_patch(self, gap: Gap, project_map: ProjectMap) -> Patch:
        if gap.gap_type == "missing_method":
            return self._plan_missing_method(gap, project_map)
        elif gap.gap_type == "missing_import":
            return self._plan_missing_import(gap, project_map)
        elif gap.gap_type == "missing_class":
            return self._plan_missing_class(gap, project_map)
        elif gap.gap_type == "missing_export":
            return self._plan_missing_export(gap, project_map)
        elif gap.gap_type == "missing_attribute":
            return self._plan_missing_attribute(gap, project_map)
        else:
            return Patch(
                patch_type="unknown",
                target_file=gap.should_be_in,
                description=f"Unknown gap type: {gap.gap_type}",
            )

    def apply_patch(self, patch: Patch, workspace: Path) -> PatchResult:
        filepath = workspace / patch.target_file
        if not filepath.exists():
            if patch.patch_type == "new_file":
                filepath.write_text(patch.code_to_insert, encoding="utf-8")
                return PatchResult(
                    success=True, file_path=patch.target_file,
                    description=patch.description,
                )
            return PatchResult(
                success=False, file_path=patch.target_file,
                description=patch.description,
                error=f"File {patch.target_file} not found",
            )

        try:
            source = filepath.read_text(encoding="utf-8")
            lines = source.splitlines(keepends=True)
        except Exception as e:
            return PatchResult(
                success=False, file_path=patch.target_file,
                description=patch.description, error=str(e),
            )

        if patch.replace_line_start > 0 and patch.replacement_code:
            start = max(0, patch.replace_line_start - 1)
            end = min(len(lines), patch.replace_line_end)
            new_lines = lines[:start] + [patch.replacement_code + "\n"] + lines[end:]
        elif patch.insert_after_line > 0 and patch.code_to_insert:
            insert_idx = min(patch.insert_after_line, len(lines))
            code = patch.code_to_insert
            if not code.endswith("\n"):
                code += "\n"
            new_lines = lines[:insert_idx] + [code] + lines[insert_idx:]
        elif patch.code_to_insert:
            code = patch.code_to_insert
            if not code.endswith("\n"):
                code += "\n"
            new_lines = lines + ["\n", code]
        else:
            return PatchResult(
                success=False, file_path=patch.target_file,
                description=patch.description, error="Empty patch",
            )

        new_source = "".join(new_lines)

        try:
            ast.parse(new_source)
        except SyntaxError as e:
            return PatchResult(
                success=False, file_path=patch.target_file,
                description=patch.description,
                error=f"Patch creates syntax error: {e}",
            )

        filepath.write_text(new_source, encoding="utf-8")
        return PatchResult(
            success=True, file_path=patch.target_file,
            description=patch.description,
        )

    def _plan_missing_method(self, gap: Gap, project_map: ProjectMap) -> Patch:
        fmap = project_map.files.get(gap.should_be_in)
        if not fmap:
            return Patch(
                patch_type="missing_method", target_file=gap.should_be_in,
                description=f"Cannot find file {gap.should_be_in}",
            )

        cmap = fmap.classes.get(gap.class_name)
        if not cmap:
            return Patch(
                patch_type="missing_method", target_file=gap.should_be_in,
                description=f"Cannot find class {gap.class_name} in {gap.should_be_in}",
            )

        insert_line = self._find_class_end(gap.should_be_in, gap.class_name, project_map)

        method_body = self._generate_method_stub(gap, cmap)

        return Patch(
            patch_type="missing_method",
            target_file=gap.should_be_in,
            description=f"Add {gap.element_name}() to {gap.class_name}",
            insert_after_line=insert_line,
            code_to_insert=method_body,
        )

    def _plan_missing_import(self, gap: Gap, project_map: ProjectMap) -> Patch:
        from autodev.import_fixer import _KNOWN_IMPORTS
        import_stmt = _KNOWN_IMPORTS.get(gap.element_name, f"import {gap.element_name}")

        return Patch(
            patch_type="missing_import",
            target_file=gap.should_be_in or gap.expected_by.split()[0],
            description=f"Add import: {import_stmt}",
            insert_after_line=0,
            code_to_insert=import_stmt,
        )

    def _plan_missing_class(self, gap: Gap, project_map: ProjectMap) -> Patch:
        target_file = gap.should_be_in
        if not target_file:
            name_lower = gap.class_name.lower()
            target_file = f"{name_lower}.py"

        class_code = f"\n\nclass {gap.class_name}:\n    pass\n"

        return Patch(
            patch_type="missing_class",
            target_file=target_file,
            description=f"Add class {gap.class_name}",
            code_to_insert=class_code,
        )

    def _plan_missing_export(self, gap: Gap, project_map: ProjectMap) -> Patch:
        fmap = project_map.files.get(gap.should_be_in)
        if not fmap:
            return Patch(
                patch_type="missing_export", target_file=gap.should_be_in,
                description=f"Cannot find {gap.should_be_in}",
            )

        for cname, cmap in fmap.classes.items():
            if gap.element_name in cmap.methods:
                return Patch(
                    patch_type="already_exists", target_file=gap.should_be_in,
                    description=f"{gap.element_name} already exists as method of {cname}",
                )

        return Patch(
            patch_type="missing_export",
            target_file=gap.should_be_in,
            description=f"Add {gap.element_name} to {gap.should_be_in}",
            code_to_insert=f"\n\ndef {gap.element_name}():\n    pass\n",
        )

    def _plan_missing_attribute(self, gap: Gap, project_map: ProjectMap) -> Patch:
        fmap = project_map.files.get(gap.should_be_in)
        if not fmap or gap.class_name not in fmap.classes:
            return Patch(
                patch_type="missing_attribute", target_file=gap.should_be_in,
                description=f"Cannot find {gap.class_name} in {gap.should_be_in}",
            )

        init_end = self._find_init_end(gap.should_be_in, gap.class_name, project_map)
        attr_code = f"        self.{gap.element_name} = None\n"

        return Patch(
            patch_type="missing_attribute",
            target_file=gap.should_be_in,
            description=f"Add self.{gap.element_name} to {gap.class_name}.__init__",
            insert_after_line=init_end,
            code_to_insert=attr_code,
        )

    def _find_class_end(self, filename: str, class_name: str, project_map: ProjectMap) -> int:
        fmap = project_map.files.get(filename)
        if not fmap:
            return 0

        try:
            source = Path(filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except Exception:
            return 0

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node.end_lineno or node.lineno + len(node.body)

        return 0

    def _find_init_end(self, filename: str, class_name: str, project_map: ProjectMap) -> int:
        try:
            source = Path(filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except Exception:
            return 0

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == "__init__":
                        return child.end_lineno or child.lineno
        return 0

    def _generate_method_stub(self, gap: Gap, cmap: ClassMap) -> str:
        method_name = gap.element_name
        params = ["self"]

        if gap.call_args:
            for i, _ in enumerate(gap.call_args):
                params.append(f"arg{i + 1}")

        if gap.call_kwargs:
            for kw in gap.call_kwargs:
                params.append(f"{kw}=None")

        if len(params) == 1 and not gap.call_args and not gap.call_kwargs:
            sig = _infer_params_from_name(method_name)
            params.extend(sig)

        param_str = ", ".join(params)
        body = _infer_body_from_name(method_name, cmap)

        return f"\n    def {method_name}({param_str}):\n{body}\n"


def _infer_params_from_name(method_name: str) -> list[str]:
    parts = method_name.split("_")
    verb = parts[0] if parts else ""

    if verb in ("add", "remove", "delete", "get", "set", "update", "borrow", "return"):
        if len(parts) > 1:
            noun = "_".join(parts[1:])
            return [noun]

    if verb in ("search", "find", "filter"):
        return ["query"]

    if verb in ("calculate", "compute", "count"):
        return []

    return []


def _infer_body_from_name(method_name: str, cmap: ClassMap) -> str:
    parts = method_name.split("_")
    verb = parts[0] if parts else ""
    noun = "_".join(parts[1:]) if len(parts) > 1 else ""

    plural = noun + "s" if noun and not noun.endswith("s") else noun
    collection = None
    for attr in cmap.attributes:
        if plural and plural in attr.lower():
            collection = attr
            break
    if not collection and cmap.attributes:
        for attr in cmap.attributes:
            if isinstance(attr, str) and attr.endswith("s"):
                collection = attr
                break

    if verb == "add" and collection:
        return f"        self.{collection}.append({noun})"
    elif verb == "remove" and collection:
        return f"        self.{collection} = [x for x in self.{collection} if x != {noun}]"
    elif verb in ("get", "find") and collection:
        return f"        return [x for x in self.{collection} if x == {noun}]"
    elif verb in ("borrow", "checkout") and collection:
        return (
            f"        for item in self.{collection}:\n"
            f"            if item == {noun}:\n"
            f"                self.{collection}.remove(item)\n"
            f"                return item\n"
            f"        return None"
        )
    elif verb == "return" and collection:
        return f"        self.{collection}.append({noun})"
    elif verb in ("count", "calculate", "compute"):
        if collection:
            return f"        return len(self.{collection})"
        return "        return 0"
    elif verb in ("search", "filter") and collection:
        return f"        return [x for x in self.{collection} if query in str(x)]"

    return "        pass"
