"""Smoke test to verify project setup."""


def test_import():
    import talos

    assert talos.__version__ == "0.1.0"
