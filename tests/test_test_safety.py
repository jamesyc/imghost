from __future__ import annotations

import pytest

from . import conftest as test_conftest


def test_test_database_default_uses_imghost_test() -> None:
    assert test_conftest.DEFAULT_TEST_DATABASE_URL.endswith("/imghost_test")


def test_assert_safe_test_database_url_rejects_live_database_name() -> None:
    with pytest.raises(RuntimeError):
        test_conftest._assert_safe_test_database_url("postgresql://imghost:imghost@localhost:5432/imghost")


def test_assert_safe_test_database_url_allows_test_database_name() -> None:
    test_conftest._assert_safe_test_database_url("postgresql://imghost:imghost@localhost:5432/imghost_test")
