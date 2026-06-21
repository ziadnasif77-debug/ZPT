"""Auto-fix common syntax errors in LLM-generated Python code.

Small LLMs produce code with unterminated strings, mismatched brackets,
and broken indentation. This module detects and fixes these issues
before the code reaches the sandbox.
"""

from __future__ import annotations

import ast
import re


def fix_syntax(source: str) -> str:
    """Attempt to fix common syntax errors in Python source code."""
    if _is_valid(source):
        return source

    fixed = source
    fixers = [
        _fix_unterminated_strings_triple,
        _fix_unterminated_strings,
        _fix_mismatched_brackets,
        _fix_trailing_backslash,
    ]
    for fixer in fixers:
        fixed = fixer(fixed)
        if _is_valid(fixed):
            return fixed

    return fixed


def _is_valid(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except SyntaxError:
        return False


def _fix_unterminated_strings_triple(source: str) -> str:
    """Convert unterminated single/double-quoted strings to triple-quoted.

    When a line has an odd number of quotes (meaning a string opened but
    not closed on the same line), replace the opening quote with triple
    quotes and find the closing quote on a later line, replacing it too.
    """
    lines = source.split("\n")
    fixed_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]
        handled = False

        for quote_char in ("'", '"'):
            triple = quote_char * 3
            if triple in line:
                continue
            count = _count_unescaped_quotes(line, quote_char)
            if count % 2 != 0:
                j = i + 1
                close_line = -1
                while j < len(lines):
                    sub_count = _count_unescaped_quotes(lines[j], quote_char)
                    if sub_count % 2 != 0:
                        close_line = j
                        break
                    j += 1

                if close_line >= 0:
                    open_idx = line.find(quote_char)
                    new_open = line[:open_idx] + triple + line[open_idx + 1:]
                    fixed_lines.append(new_open)

                    for k in range(i + 1, close_line):
                        fixed_lines.append(lines[k])

                    cl = lines[close_line]
                    close_idx = cl.rfind(quote_char)
                    new_close = cl[:close_idx] + triple + cl[close_idx + 1:]
                    fixed_lines.append(new_close)

                    i = close_line + 1
                    handled = True
                    break

        if not handled:
            fixed_lines.append(line)
            i += 1

    result = "\n".join(fixed_lines)
    if _is_valid(result):
        return result
    return source


def _fix_unterminated_strings(source: str) -> str:
    """Fix single-quoted strings that span multiple lines.

    LLMs generate things like:
        print('1. Add Task
        2. Delete Task
        3. Exit')

    This converts them to triple-quoted strings or joins the lines.
    """
    lines = source.split("\n")
    fixed_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        for quote_char in ("'", '"'):
            count = _count_unescaped_quotes(line, quote_char)
            if count % 2 != 0:
                triple = quote_char * 3
                if triple not in line:
                    collected = [line]
                    j = i + 1
                    closed = False
                    while j < len(lines):
                        collected.append(lines[j])
                        sub_count = _count_unescaped_quotes(lines[j], quote_char)
                        if sub_count % 2 != 0:
                            closed = True
                            break
                        j += 1

                    if closed:
                        combined = "\\n".join(
                            l.rstrip() for l in collected
                        )
                        idx = line.find(quote_char)
                        before_quote = line[:idx]
                        after_first_quote = combined[idx + 1:]

                        last_quote_idx = after_first_quote.rfind(quote_char)
                        if last_quote_idx >= 0:
                            content = after_first_quote[:last_quote_idx]
                            after_last_quote = after_first_quote[last_quote_idx + 1:]
                            content = content.replace(quote_char, "\\" + quote_char)
                            fixed_line = (
                                before_quote
                                + quote_char
                                + content
                                + quote_char
                                + after_last_quote
                            )
                            fixed_lines.append(fixed_line)
                            i = j + 1
                            break
                    else:
                        pass
        else:
            fixed_lines.append(line)
            i += 1
            continue
        continue

    result = "\n".join(fixed_lines)
    if _is_valid(result):
        return result
    return source


def _count_unescaped_quotes(line: str, quote: str) -> int:
    """Count quote characters not preceded by backslash and not in triple quotes."""
    triple = quote * 3
    if triple in line:
        return 0

    count = 0
    i = 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            i += 2
            continue
        if line[i] == quote:
            count += 1
        i += 1
    return count


def _fix_mismatched_brackets(source: str) -> str:
    """Add missing closing brackets/parens at end of file."""
    stack = []
    match = {"(": ")", "[": "]", "{": "}"}
    in_string = False
    string_char = None
    i = 0
    text = source

    while i < len(text):
        ch = text[i]

        if ch == "\\" and in_string:
            i += 2
            continue

        if not in_string:
            if ch in ("'", '"'):
                if text[i:i+3] in ('"""', "'''"):
                    end = text.find(text[i:i+3], i + 3)
                    if end >= 0:
                        i = end + 3
                    else:
                        i = len(text)
                    continue
                in_string = True
                string_char = ch
            elif ch in match:
                stack.append(match[ch])
            elif ch in (")", "]", "}"):
                if stack and stack[-1] == ch:
                    stack.pop()
        else:
            if ch == string_char:
                in_string = False

        i += 1

    if stack:
        closing = "".join(reversed(stack))
        result = source.rstrip() + closing + "\n"
        if _is_valid(result):
            return result

    return source


def _fix_trailing_backslash(source: str) -> str:
    """Remove trailing backslashes at end of file that cause syntax errors."""
    if source.rstrip().endswith("\\"):
        result = source.rstrip().rstrip("\\") + "\n"
        if _is_valid(result):
            return result
    return source
