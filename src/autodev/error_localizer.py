"""Error Localizer — parses test output to find the TRUE source of failure.

Distinguishes between where an error CRASHED (test file) and where the
TRUE SOURCE is (missing method, wrong import, etc.). Uses AST analysis
to verify findings against actual code.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ErrorLocation:
    error_type: str = ""
    message: str = ""
    true_source_file: str = ""
    true_source_line: int = 0
    true_source_class: str = ""
    missing_element: str = ""
    element_type: str = ""
    crash_file: str = ""
    crash_line: int = 0
    full_traceback: str = ""


class ErrorCategory:
    MISSING_METHOD = "missing_method"
    MISSING_IMPORT = "missing_import"
    WRONG_LOGIC = "wrong_logic"
    SYNTAX_ERROR = "syntax_error"
    MISSING_CLASS = "missing_class"
    WRONG_SIGNATURE = "wrong_signature"
    MISSING_ATTRIBUTE = "missing_attribute"
    UNKNOWN = "unknown"


class ErrorLocalizer:
    def localize(self, stderr: str, stdout: str, workspace: Path | None = None) -> ErrorLocation:
        combined = stderr + "\n" + stdout

        loc = ErrorLocation(full_traceback=combined[:2000])

        self._extract_traceback_locations(combined, loc)

        if "AttributeError" in combined:
            self._parse_attribute_error(combined, loc, workspace)
        elif "ImportError" in combined or "ModuleNotFoundError" in combined:
            self._parse_import_error(combined, loc)
        elif "NameError" in combined:
            self._parse_name_error(combined, loc)
        elif "TypeError" in combined:
            self._parse_type_error(combined, loc, workspace)
        elif "SyntaxError" in combined:
            self._parse_syntax_error(combined, loc)
        elif "AssertionError" in combined or "AssertError" in combined:
            self._parse_assertion_error(combined, loc)
        else:
            loc.error_type = "RuntimeError"
            m = re.search(r"(\w+Error):\s*(.+?)(?:\n|$)", combined)
            if m:
                loc.error_type = m.group(1)
                loc.message = m.group(2).strip()

        return loc

    def classify(self, error: ErrorLocation) -> str:
        if error.error_type == "AttributeError":
            if error.element_type == "method":
                return ErrorCategory.MISSING_METHOD
            return ErrorCategory.MISSING_ATTRIBUTE
        if error.error_type in ("ImportError", "ModuleNotFoundError"):
            return ErrorCategory.MISSING_IMPORT
        if error.error_type == "NameError":
            return ErrorCategory.MISSING_IMPORT
        if error.error_type == "TypeError" and "argument" in error.message.lower():
            return ErrorCategory.WRONG_SIGNATURE
        if error.error_type == "SyntaxError":
            return ErrorCategory.SYNTAX_ERROR
        if error.error_type in ("AssertionError", "AssertError"):
            return ErrorCategory.WRONG_LOGIC
        return ErrorCategory.UNKNOWN

    def _extract_traceback_locations(self, text: str, loc: ErrorLocation):
        file_lines = re.findall(r'File "([^"]+)", line (\d+)', text)
        if not file_lines:
            return

        crash_file, crash_line = file_lines[-1]
        loc.crash_file = Path(crash_file).name
        loc.crash_line = int(crash_line)

        for fpath, line in reversed(file_lines):
            fname = Path(fpath).name
            if not fname.startswith("test_") and fname != "test_runner.py":
                loc.true_source_file = fname
                loc.true_source_line = int(line)
                break

        if not loc.true_source_file:
            loc.true_source_file = loc.crash_file
            loc.true_source_line = loc.crash_line

    def _parse_attribute_error(self, text: str, loc: ErrorLocation, workspace: Path | None):
        loc.error_type = "AttributeError"

        m = re.search(
            r"AttributeError:\s*['\"](\w+)['\"]\s+object has no attribute\s+['\"](\w+)['\"]",
            text,
        )
        if m:
            class_name = m.group(1)
            attr_name = m.group(2)
            loc.true_source_class = class_name
            loc.missing_element = attr_name
            loc.message = f"'{class_name}' has no attribute '{attr_name}'"

            loc.element_type = self._infer_element_type_from_text(text, attr_name)
            if workspace:
                refined = self._infer_element_type(
                    workspace, class_name, attr_name, text,
                )
                if refined == "method":
                    loc.element_type = "method"
                if not loc.true_source_file or loc.true_source_file.startswith("test"):
                    loc.true_source_file = self._find_class_file(workspace, class_name)
        else:
            m2 = re.search(r"AttributeError:\s*(.+?)(?:\n|$)", text)
            if m2:
                loc.message = m2.group(1).strip()

    def _parse_import_error(self, text: str, loc: ErrorLocation):
        loc.error_type = "ImportError"

        m = re.search(
            r"(?:ImportError|ModuleNotFoundError):\s*(?:No module named\s+)?'?([^'\"]+)'?",
            text,
        )
        if m:
            loc.missing_element = m.group(1).strip()
            loc.element_type = "import"
            loc.message = f"Cannot import '{loc.missing_element}'"

        m2 = re.search(r"cannot import name '(\w+)' from '(\w+)'", text)
        if m2:
            loc.missing_element = m2.group(1)
            loc.true_source_file = m2.group(2) + ".py"
            loc.element_type = "import"
            loc.message = f"Cannot import '{m2.group(1)}' from '{m2.group(2)}'"

    def _parse_name_error(self, text: str, loc: ErrorLocation):
        loc.error_type = "NameError"
        m = re.search(r"NameError:\s*name '(\w+)' is not defined", text)
        if m:
            loc.missing_element = m.group(1)
            loc.element_type = "import"
            loc.message = f"Name '{m.group(1)}' is not defined (missing import?)"

    def _parse_type_error(self, text: str, loc: ErrorLocation, workspace: Path | None):
        loc.error_type = "TypeError"

        m = re.search(
            r"TypeError:\s*(\w+)\(\)\s*got an unexpected keyword argument\s+'(\w+)'",
            text,
        )
        if m:
            loc.true_source_class = m.group(1)
            loc.missing_element = m.group(2)
            loc.element_type = "argument"
            loc.message = f"{m.group(1)}() got unexpected keyword argument '{m.group(2)}'"
            if workspace:
                loc.true_source_file = self._find_class_file(workspace, m.group(1))
            return

        m2 = re.search(
            r"TypeError:\s*(\w+)\(\)\s*takes\s+(\d+)\s+positional argument.*?but\s+(\d+)",
            text,
        )
        if m2:
            loc.true_source_class = m2.group(1)
            loc.element_type = "argument"
            loc.message = f"{m2.group(1)}() takes {m2.group(2)} args but {m2.group(3)} given"
            return

        m3 = re.search(r"TypeError:\s*(.+?)(?:\n|$)", text)
        if m3:
            loc.message = m3.group(1).strip()

    def _parse_syntax_error(self, text: str, loc: ErrorLocation):
        loc.error_type = "SyntaxError"
        m = re.search(r'File "([^"]+)", line (\d+).*?\n.*?SyntaxError:\s*(.+?)(?:\n|$)', text, re.DOTALL)
        if m:
            loc.true_source_file = Path(m.group(1)).name
            loc.true_source_line = int(m.group(2))
            loc.message = m.group(3).strip()
            loc.element_type = "syntax"

    def _parse_assertion_error(self, text: str, loc: ErrorLocation):
        loc.error_type = "AssertionError"
        loc.element_type = "logic"

        m = re.search(r"AssertionError:\s*(.+?)(?:\n|$)", text)
        if m:
            loc.message = m.group(1).strip()
        else:
            m2 = re.search(r"assert\s+(.+?)(?:\n|$)", text)
            if m2:
                loc.message = f"Assertion failed: {m2.group(1).strip()}"

    def _infer_element_type_from_text(self, text: str, attr_name: str) -> str:
        """Infer if the missing attribute is a method by checking call patterns in the traceback."""
        if re.search(rf"\.{re.escape(attr_name)}\s*\(", text):
            return "method"
        return "attribute"

    def _infer_element_type(
        self, workspace: Path, class_name: str, attr_name: str, text: str
    ) -> str:
        if re.search(rf"\.{attr_name}\s*\(", text):
            return "method"

        for py_file in workspace.glob("*.py"):
            if py_file.name.startswith("test"):
                try:
                    source = py_file.read_text(encoding="utf-8")
                    if re.search(rf"\.{attr_name}\s*\(", source):
                        return "method"
                except Exception:
                    continue

        return "attribute"

    def _find_class_file(self, workspace: Path, class_name: str) -> str:
        for py_file in workspace.glob("*.py"):
            if py_file.name.startswith("test") or py_file.name == "test_runner.py":
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name == class_name:
                        return py_file.name
            except (SyntaxError, Exception):
                continue
        return ""
