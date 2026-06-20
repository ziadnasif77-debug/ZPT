"""Auto-fix missing imports in generated Python code.

Small LLMs consistently forget imports (datetime, json, Enum, etc.).
This module detects undefined names and injects the correct imports.
"""

from __future__ import annotations

import ast
import re

_KNOWN_IMPORTS: dict[str, str] = {
    "Enum": "from enum import Enum",
    "IntEnum": "from enum import IntEnum",
    "auto": "from enum import auto",
    "dataclass": "from dataclasses import dataclass",
    "field": "from dataclasses import field",
    "dataclasses": "import dataclasses",
    "datetime": "from datetime import datetime",
    "timedelta": "from datetime import timedelta",
    "date": "from datetime import date",
    "time": "from datetime import time",
    "json": "import json",
    "os": "import os",
    "sys": "import sys",
    "re": "import re",
    "math": "import math",
    "random": "import random",
    "pathlib": "import pathlib",
    "Path": "from pathlib import Path",
    "typing": "import typing",
    "Optional": "from typing import Optional",
    "List": "from typing import List",
    "Dict": "from typing import Dict",
    "Tuple": "from typing import Tuple",
    "Set": "from typing import Set",
    "Any": "from typing import Any",
    "Union": "from typing import Union",
    "copy": "import copy",
    "deepcopy": "from copy import deepcopy",
    "defaultdict": "from collections import defaultdict",
    "Counter": "from collections import Counter",
    "OrderedDict": "from collections import OrderedDict",
    "namedtuple": "from collections import namedtuple",
    "ABC": "from abc import ABC",
    "abstractmethod": "from abc import abstractmethod",
    "contextmanager": "from contextlib import contextmanager",
    "functools": "import functools",
    "itertools": "import itertools",
    "hashlib": "import hashlib",
    "uuid": "import uuid",
    "uuid4": "from uuid import uuid4",
    "tempfile": "import tempfile",
    "shutil": "import shutil",
    "glob": "import glob",
    "csv": "import csv",
    "io": "import io",
    "StringIO": "from io import StringIO",
    "BytesIO": "from io import BytesIO",
    "subprocess": "import subprocess",
    "threading": "import threading",
    "logging": "import logging",
    "unittest": "import unittest",
    "pytest": "import pytest",
    "sqlite3": "import sqlite3",
    "pickle": "import pickle",
    "struct": "import struct",
    "socket": "import socket",
    "http": "import http",
    "urllib": "import urllib",
    "base64": "import base64",
    "hmac": "import hmac",
    "secrets": "import secrets",
    "string": "import string",
    "textwrap": "import textwrap",
    "pprint": "import pprint",
    "traceback": "import traceback",
    "inspect": "import inspect",
    "platform": "import platform",
    "argparse": "import argparse",
    "enum": "import enum",
    "collections": "import collections",
    "abc": "import abc",
}


def _get_existing_imports(source: str) -> set[str]:
    """Get all names that are already imported in the source."""
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _get_defined_names(source: str) -> set[str]:
    """Get all names defined in the source (classes, functions, variables)."""
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                names.add(arg.arg)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.For):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _get_used_names(source: str) -> set[str]:
    """Get all names used (loaded) in the source."""
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                names.add(root.id)
    return names


def fix_imports(source: str) -> str:
    """Add missing imports to Python source code.

    Detects undefined names that match known stdlib modules/classes
    and injects the correct import statements at the top of the file.
    """
    existing = _get_existing_imports(source)
    defined = _get_defined_names(source)
    used = _get_used_names(source)

    available = existing | defined | {"self", "cls", "True", "False", "None",
                                       "print", "len", "range", "str", "int",
                                       "float", "bool", "list", "dict", "set",
                                       "tuple", "type", "super", "property",
                                       "staticmethod", "classmethod", "isinstance",
                                       "issubclass", "hasattr", "getattr", "setattr",
                                       "delattr", "callable", "iter", "next", "zip",
                                       "map", "filter", "sorted", "reversed",
                                       "enumerate", "min", "max", "sum", "abs",
                                       "round", "pow", "divmod", "hash", "id",
                                       "repr", "format", "chr", "ord", "hex",
                                       "oct", "bin", "open", "input", "vars",
                                       "dir", "help", "exec", "eval", "compile",
                                       "globals", "locals", "any", "all",
                                       "ValueError", "TypeError", "KeyError",
                                       "IndexError", "AttributeError", "ImportError",
                                       "FileNotFoundError", "IOError", "OSError",
                                       "RuntimeError", "StopIteration", "Exception",
                                       "BaseException", "NotImplementedError",
                                       "ZeroDivisionError", "OverflowError",
                                       "AssertionError", "AssertionError",
                                       "NameError", "SyntaxError", "IndentationError",
                                       "ModuleNotFoundError", "PermissionError",
                                       "IsADirectoryError", "FileExistsError",
                                       "ConnectionError", "TimeoutError",
                                       "UnicodeDecodeError", "UnicodeEncodeError",
                                       "object", "NotImplemented", "Ellipsis",
                                       "__name__", "__file__", "__doc__",
                                       "__all__", "__init__", "__main__",
                                       "breakpoint", "exit", "quit",
                                       "complex", "bytes", "bytearray",
                                       "memoryview", "frozenset", "slice",
                                       "staticmethod", "classmethod",
                                       "AssertionError"}

    missing = used - available

    imports_to_add: list[str] = []
    for name in sorted(missing):
        if name in _KNOWN_IMPORTS:
            imp = _KNOWN_IMPORTS[name]
            if imp not in imports_to_add:
                imports_to_add.append(imp)

    if not imports_to_add:
        return source

    lines = source.split("\n")

    insert_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            insert_idx = i + 1
            if stripped.startswith('"""') or stripped.startswith("'''"):
                quote = stripped[:3]
                if stripped.count(quote) < 2:
                    for j in range(i + 1, len(lines)):
                        if quote in lines[j]:
                            insert_idx = j + 1
                            break
            continue
        if stripped.startswith("from __future__"):
            insert_idx = i + 1
            continue
        if stripped.startswith("import ") or stripped.startswith("from "):
            insert_idx = i + 1
            continue
        if stripped == "":
            if insert_idx == i:
                insert_idx = i + 1
            continue
        break

    import_block = "\n".join(imports_to_add)
    lines.insert(insert_idx, import_block)

    return "\n".join(lines)
