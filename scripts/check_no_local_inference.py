#!/usr/bin/env python
"""Guard: no LLM or ML runtime may ever be installed or imported (D-13).

Why this exists
---------------
The application runs on an 8 GB Windows laptop with no GPU. Every byte of
inference happens on someone else's hardware — Google Colab during development,
and Vays Infotech's own LLM after handover.

A comment in the README does not stop a future developer from running
``pip install transformers`` because they want "just the tokenizer" and
accidentally pulling 2 GB of PyTorch onto a machine that cannot afford it. A
failing build does. This script is the difference between a design intention and
a property of the system — and it is the part that survives whoever wrote it
leaving the project.

What it checks
--------------
1. ``requirements*.txt`` — no banned distribution appears as a dependency.
2. Every ``.py`` file in the source tree — no banned module is imported.

Import detection uses :mod:`ast`, so a banned name appearing inside a string,
a comment or a docstring (as it does throughout this file) is not a false
positive. Only real ``import`` statements count.

Usage
-----
    python scripts/check_no_local_inference.py

Exit codes: 0 = clean, 1 = violation found.

Wired into CI and ``.pre-commit-config.yaml``. Also mirrored as an
``import-linter`` contract in ``pyproject.toml`` — belt and braces, because this
is the one rule that must not quietly stop working.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Distributions and top-level modules that would place inference on this machine.
#: Stored in normalised form (lowercase, hyphens) — see :func:`_normalise`.
BANNED: frozenset[str] = frozenset(
    {
        # deep-learning frameworks
        "torch",
        "torchvision",
        "torchaudio",
        "tensorflow",
        "tensorflow-cpu",
        "jax",
        "jaxlib",
        "flax",
        "paddlepaddle",
        # model runtimes / loaders
        "transformers",
        "sentence-transformers",
        "accelerate",
        "optimum",
        "bitsandbytes",
        "peft",
        "diffusers",
        # local inference servers and bindings
        "llama-cpp-python",
        "llama-cpp",
        "ctransformers",
        "exllamav2",
        "autoawq",
        "auto-gptq",
        "gpt4all",
        "vllm",
        "ollama",
        "mlx",
        "mlx-lm",
        # inference engines
        "onnxruntime",
        "onnxruntime-gpu",
        "openvino",
        "tensorrt",
    }
)

#: Directories never scanned.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "data",
        "logs",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
    }
)

#: Requirement line -> distribution name. Handles ``pkg==1.0``, ``pkg[extra]>=2``,
#: ``pkg ; python_version < "3.12"`` and inline comments.
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalise(name: str) -> str:
    """PEP 503 normalisation, with underscores folded to hyphens.

    Lets a single ``BANNED`` entry match both the distribution name
    (``llama-cpp-python``) and the import name (``llama_cpp``).
    """
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _iter_python_files() -> list[Path]:
    return [
        path
        for path in PROJECT_ROOT.rglob("*.py")
        if not any(part in SKIP_DIRS for part in path.parts)
    ]


def check_requirements() -> list[str]:
    """Return a violation message for each banned distribution in requirements."""
    violations: list[str] = []
    for req_file in sorted(PROJECT_ROOT.glob("requirements*.txt")):
        for lineno, raw in enumerate(req_file.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = _REQ_NAME.match(line)
            if match and _normalise(match.group(1)) in BANNED:
                rel = req_file.relative_to(PROJECT_ROOT)
                violations.append(f"{rel}:{lineno}: banned dependency '{match.group(1)}'")
    return violations


def _imported_roots(tree: ast.AST) -> set[tuple[str, int]]:
    """Collect ``(top_level_module, lineno)`` for every import in a parsed file."""
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], node.lineno))
        # `level > 0` is a relative import — always first-party, never banned.
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add((node.module.split(".")[0], node.lineno))
    return found


def check_imports() -> list[str]:
    """Return a violation message for each banned import in the source tree."""
    violations: list[str] = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{path.relative_to(PROJECT_ROOT)}: could not parse ({exc.msg})")
            continue
        for module, lineno in sorted(_imported_roots(tree), key=lambda item: item[1]):
            if _normalise(module) in BANNED:
                rel = path.relative_to(PROJECT_ROOT)
                violations.append(f"{rel}:{lineno}: banned import '{module}'")
    return violations


def main() -> int:
    violations = check_requirements() + check_imports()

    if violations:
        print("=" * 78)
        print("BUILD FAILED — local inference dependency detected (D-13)")
        print("=" * 78)
        for violation in violations:
            print(f"  {violation}")
        print()
        print("This project must never run a model on the developer's machine.")
        print("All inference is remote: Google Colab now, Vays' own LLM after handover.")
        print()
        print("If you need token counts, use core.tokenizer (a calibrated heuristic).")
        print("If you need a model, call it over HTTP through modules/ai/ instead.")
        print("See docs/09_FINAL_DECISIONS.md section G1.")
        return 1

    scanned = len(_iter_python_files())
    print(
        f"OK - no local inference dependencies ({scanned} Python files, "
        f"{len(list(PROJECT_ROOT.glob('requirements*.txt')))} requirement files scanned)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
