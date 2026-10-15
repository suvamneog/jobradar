"""Focused tests for configuration-loading errors."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobradar.main import load_config


def expect_config_error(path: Path, expected: str) -> None:
    try:
        load_config(path)
    except SystemExit as exc:
        message = str(exc)
        assert expected in message, f"Expected {expected!r} in {message!r}"
    else:
        raise AssertionError(f"Expected load_config({path}) to exit")


def run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Missing-file behavior already has an actionable message.
        missing = root / "missing.yaml"
        expect_config_error(missing, "Copy config.example.yaml to config.yaml")

        # Empty YAML parses to None; reject it before callers get a NoneType error.
        empty = root / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        expect_config_error(empty, "is empty or is not a YAML mapping")

        # YAML that parses successfully but isn't a mapping is not usable config.
        non_mapping = root / "list.yaml"
        non_mapping.write_text("- item\n", encoding="utf-8")
        expect_config_error(non_mapping, "is empty or is not a YAML mapping")

        valid = root / "valid.yaml"
        valid.write_text("llm:\n  provider: none\n", encoding="utf-8")
        assert load_config(valid) == {"llm": {"provider": "none"}}

    print("config tests PASS")


if __name__ == "__main__":
    run()
