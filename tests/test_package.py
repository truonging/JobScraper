"""Verify that the source package is installed and importable."""

from importlib import import_module


def test_package_imports():
    assert import_module("job_matcher").__name__ == "job_matcher"
