"""Tests for CMP Report Type selection and monthly date computation."""

from pathlib import Path
from typing import Any

import pytest

from network_report.collectors.cmp_crawler import (
    _set_report_type_and_date,
    compute_monthly_date_range,
    select_report_type,
)


class FakeLocator:
    """Stub for Playwright sync Locator."""

    def __init__(
        self,
        value: str = "",
        text: str = "",
        count: int = 1,
        items: list["FakeLocator"] | None = None,
        sub_locators: dict[str, "FakeLocator"] | None = None,
    ) -> None:
        self._value = value
        self._text = text
        self._count = count if items is None else len(items)
        self._items = items or []
        self._sub_locators = sub_locators or {}
        self.clicks = 0
        self.waited_states: list[str] = []

    def count(self) -> int:
        return len(self._items) if self._items else self._count

    def nth(self, index: int) -> "FakeLocator":
        if self._items and 0 <= index < len(self._items):
            return self._items[index]
        return self

    @property
    def first(self) -> "FakeLocator":
        if self._items:
            return self._items[0]
        return self

    def input_value(self) -> str:
        return self._value

    def inner_text(self, timeout: int | None = None) -> str:
        return self._text

    def is_visible(self) -> bool:
        return True

    def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        self.waited_states.append(state)

    def click(self, *args: Any, **kwargs: Any) -> None:
        self.clicks += 1

    def locator(self, selector: str) -> "FakeLocator":
        for pattern, loc in self._sub_locators.items():
            if pattern in selector:
                return loc
        return FakeLocator(count=0)

    def filter(self, has_text: Any = None, **kwargs: Any) -> "FakeLocator":
        if has_text is None:
            return self

        matched_items: list[FakeLocator] = []
        candidates = self._items if self._items else ([self] if self._count > 0 else [])

        for item in candidates:
            item_text = item.inner_text()
            if hasattr(has_text, "search"):
                if has_text.search(item_text):
                    matched_items.append(item)
            elif str(has_text).lower() in item_text.lower():
                matched_items.append(item)

        if matched_items:
            return FakeLocator(items=matched_items)
        return FakeLocator(count=0)


class FakePage:
    """Stub for Playwright sync Page."""

    def __init__(self, selector_map: dict[str, FakeLocator] | None = None) -> None:
        self._selector_map = selector_map or {}
        self.keyboard = self

    def locator(self, selector: str) -> FakeLocator:
        for key, loc in self._selector_map.items():
            if key in selector:
                return loc
        return FakeLocator(count=0)

    def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        pass

    def wait_for_timeout(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_default_timeout(self, *args: Any, **kwargs: Any) -> None:
        pass

    def press(self, key: str) -> None:
        pass


# -----------------------------------------------------------------------------
# 1. Monthly date range computation tests
# -----------------------------------------------------------------------------


def test_compute_monthly_date_range_standard_month():
    start, end = compute_monthly_date_range("2026-10")
    assert start == "2026-10-01"
    assert end == "2026-10-31"


def test_compute_monthly_date_range_30_day_month():
    start, end = compute_monthly_date_range("2026-04")
    assert start == "2026-04-01"
    assert end == "2026-04-30"


def test_compute_monthly_date_range_february_non_leap():
    start, end = compute_monthly_date_range("2026-02")
    assert start == "2026-02-01"
    assert end == "2026-02-28"


def test_compute_monthly_date_range_february_leap_year():
    start, end = compute_monthly_date_range("2024-02")
    assert start == "2024-02-01"
    assert end == "2024-02-29"


def test_compute_monthly_date_range_with_day_specified():
    start, end = compute_monthly_date_range("2026-11-15")
    assert start == "2026-11-01"
    assert end == "2026-11-30"


def test_compute_monthly_date_range_invalid_format():
    with pytest.raises(ValueError, match="Invalid target_month format"):
        compute_monthly_date_range("invalid")


# -----------------------------------------------------------------------------
# 2. select_report_type stub tests
# -----------------------------------------------------------------------------


def test_select_report_type_already_daily():
    """When the filter already shows 'Daily', returns True without clicking anything."""
    daily_input = FakeLocator(value="Daily")
    filter_inputs = FakeLocator(items=[daily_input])
    btn = FakeLocator()

    page = FakePage(
        {
            "input.v-filterselect-input": filter_inputs,
            "v-filterselect-button": btn,
        }
    )

    result = select_report_type(page, "Daily")
    assert result is True
    assert btn.clicks == 0


def test_select_report_type_already_monthly():
    """When the filter already shows 'Monthly', returns True without clicking anything."""
    monthly_input = FakeLocator(value="Monthly")
    filter_inputs = FakeLocator(items=[monthly_input])
    btn = FakeLocator()

    page = FakePage(
        {
            "input.v-filterselect-input": filter_inputs,
            "v-filterselect-button": btn,
        }
    )

    result = select_report_type(page, "Monthly")
    assert result is True
    assert btn.clicks == 0


def test_select_report_type_monthly_to_daily():
    """Filter shows 'Monthly', row text contains 'Report Type' -> clicks button and selects 'Daily'."""
    btn = FakeLocator()
    ancestor_div = FakeLocator(sub_locators={"v-filterselect-button": btn})
    row_container = FakeLocator(text="Report Type: Monthly")

    monthly_input = FakeLocator(
        value="Monthly",
        sub_locators={
            "ancestor::div": ancestor_div,
            "ancestor::*": row_container,
        },
    )
    filter_inputs = FakeLocator(items=[monthly_input])

    daily_item = FakeLocator(text="Daily")
    overlay_menu = FakeLocator(items=[daily_item])

    def custom_locator(selector: str) -> FakeLocator:
        if "input.v-filterselect-input" in selector:
            return filter_inputs
        if any(
            marker in selector
            for marker in ["gwt-MenuItem", "v-select-item", "v-filterselect-suggestmenu"]
        ):
            return overlay_menu
        if "v-filterselect-button" in selector:
            return btn
        return FakeLocator(count=0)

    page = FakePage()
    page.locator = custom_locator

    result = select_report_type(page, "Daily")
    assert result is True
    assert btn.clicks == 1
    assert daily_item.clicks == 1


def test_select_report_type_no_filterselect_inputs():
    """No input.v-filterselect-input present -> returns False without exception."""
    page = FakePage({"input.v-filterselect-input": FakeLocator(count=0)})
    result = select_report_type(page, "Daily")
    assert result is False


def test_select_report_type_overlay_item_missing():
    """Overlay opened but requested item is missing -> returns False without exception."""
    btn = FakeLocator()
    ancestor_div = FakeLocator(sub_locators={"v-filterselect-button": btn})
    row_container = FakeLocator(text="Report Type:")

    monthly_input = FakeLocator(
        value="Monthly",
        sub_locators={
            "ancestor::div": ancestor_div,
            "ancestor::*": row_container,
        },
    )
    filter_inputs = FakeLocator(items=[monthly_input])

    # Overlay contains unrelated options, no 'Daily'
    other_item = FakeLocator(text="Other Report")
    overlay_menu = FakeLocator(items=[other_item])

    def custom_locator(selector: str) -> FakeLocator:
        if "input.v-filterselect-input" in selector:
            return filter_inputs
        if any(
            marker in selector
            for marker in ["gwt-MenuItem", "v-select-item", "v-filterselect-suggestmenu"]
        ):
            return overlay_menu
        if "v-filterselect-button" in selector:
            return btn
        return FakeLocator(count=0)

    page = FakePage()
    page.locator = custom_locator

    result = select_report_type(page, "Daily")
    assert result is False
    assert btn.clicks == 1
    assert other_item.clicks == 0


# -----------------------------------------------------------------------------
# 3. Exporter calling select_report_type with Daily and Monthly
# -----------------------------------------------------------------------------


def test_exporter_calls_select_report_type_daily_and_monthly(monkeypatch):
    """Test that _set_report_type_and_date calls select_report_type with 'Daily' and 'Monthly'."""
    calls: list[str] = []

    def mock_select_report_type(page, report_type, timeout_ms=30000):
        calls.append(report_type)
        return True

    filled_dates: list[tuple[str, str]] = []

    def mock_fill_query_dates(page, start_date, end_date, timeout_ms=15000):
        filled_dates.append((start_date, end_date))
        return True

    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler.select_report_type",
        mock_select_report_type,
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._fill_query_dates",
        mock_fill_query_dates,
    )

    page = FakePage()

    # Call with Daily
    res_daily = _set_report_type_and_date(page, "Daily", "2026-10-12", 30000)
    assert res_daily is True
    assert calls[-1] == "Daily"
    assert filled_dates[-1] == ("2026-10-12", "2026-10-12")

    # Call with Monthly
    res_monthly = _set_report_type_and_date(page, "Monthly", "2026-10", 30000)
    assert res_monthly is True
    assert calls[-1] == "Monthly"
    assert filled_dates[-1] == ("2026-10-01", "2026-10-31")


# -----------------------------------------------------------------------------
# 4. End-to-end export file destination and diagnostic logging tests
# -----------------------------------------------------------------------------


def test_run_cmp_crawler_saves_to_iot_raw_dir(monkeypatch, tmp_path):
    """Verify that daily and monthly exported XLSX files land in output/raw/iot/{YYYYMMDD}/."""
    from network_report.collectors.cmp_crawler import run_cmp_crawler

    class FakeBrowser:
        def new_context(self, *a, **kw):
            return self

        def new_page(self):
            return FakePage()

        def set_default_timeout(self, *a):
            pass

        def close(self):
            pass

    class FakePlaywright:
        class firefox:
            @staticmethod
            def launch(**kwargs):
                return FakeBrowser()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("network_report.collectors.cmp_crawler.sync_playwright", FakePlaywright)
    monkeypatch.setattr("network_report.collectors.cmp_crawler._login_cmp", lambda *a, **kw: True)
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._navigate_to_dashboard", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._save_dashboard_screenshot_and_sidecar",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._handle_2fa_if_needed", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._navigate_to_usage_query", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._set_report_type_and_date", lambda *a, **kw: True
    )

    temp_files = []

    def mock_sort_and_export(page, timeout):
        t = tmp_path / f"temp_{len(temp_files)}.xlsx"
        t.write_text("fake xlsx content")
        temp_files.append(t)
        return t

    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._sort_and_export", mock_sort_and_export
    )

    result = run_cmp_crawler(
        base_url="https://ep.iotcc.telkomsel.com",
        username="user",
        password="pwd",
        target_date="2026-10-12",
        target_month="2026-10",
        output_dir=tmp_path,
        headless=True,
        monthly_export_enabled=True,
    )

    expected_dir = tmp_path / "raw" / "iot" / "20261012"
    expected_daily = expected_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx"
    expected_monthly = expected_dir / "report_202610_MONTHLY_USAGE_by_SIM.xlsx"

    assert result.daily_file == expected_daily
    assert result.monthly_file == expected_monthly
    assert expected_daily.exists()
    assert expected_monthly.exists()


def test_run_cmp_crawler_failure_logging(monkeypatch, tmp_path, caplog):
    """When a report type fails, verify failure diagnostics are logged."""
    import logging
    from network_report.collectors.cmp_crawler import run_cmp_crawler

    class FakeBrowser:
        def new_context(self, *a, **kw):
            return self

        def new_page(self):
            return FakePage()

        def set_default_timeout(self, *a):
            pass

        def close(self):
            pass

    class FakePlaywright:
        class firefox:
            @staticmethod
            def launch(**kwargs):
                return FakeBrowser()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("network_report.collectors.cmp_crawler.sync_playwright", FakePlaywright)
    monkeypatch.setattr("network_report.collectors.cmp_crawler._login_cmp", lambda *a, **kw: True)
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._navigate_to_dashboard", lambda *a, **kw: False
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._handle_2fa_if_needed", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._navigate_to_usage_query", lambda *a, **kw: True
    )

    # Fail Daily, succeed Monthly but fail download
    def mock_set_report_type(page, report_type, target, timeout):
        if report_type == "Daily":
            return False
        return True

    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._set_report_type_and_date",
        mock_set_report_type,
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._sort_and_export",
        lambda *a, **kw: None,
    )

    with caplog.at_level(logging.WARNING):
        result = run_cmp_crawler(
            base_url="https://ep.iotcc.telkomsel.com",
            username="user",
            password="pwd",
            target_date="2026-10-12",
            target_month="2026-10",
            output_dir=tmp_path,
            headless=True,
            monthly_export_enabled=True,
        )

    assert result.daily_file is None
    assert result.monthly_file is None
    assert "Daily export failed: could not set report type 'Daily'" in caplog.text
    assert "Monthly export failed: download did not produce a file" in caplog.text


def test_sort_and_export_with_icomoon_icon(tmp_path):
    from network_report.collectors.cmp_crawler import _sort_and_export

    clicked = []

    class MockDownload:
        suggested_filename = "usage_export.xlsx"

        def save_as(self, path):
            Path(path).write_text("exported_xlsx_data")

    class MockDownloadInfo:
        value = MockDownload()

    class MockExpectDownload:
        def __enter__(self):
            return MockDownloadInfo()

        def __exit__(self, *args):
            pass

    class MockLocator:
        def __init__(self, selector, exists=True, visible=True):
            self.selector = selector
            self._exists = exists
            self._visible = visible

        @property
        def first(self):
            return self

        def locator(self, sub_sel):
            return MockLocator(f"{self.selector} >> {sub_sel}", self._exists, self._visible)

        def filter(self, *args, **kwargs):
            return self

        def count(self):
            return 1 if self._exists else 0

        def is_visible(self):
            return self._visible

        def click(self, timeout=None, force=False):
            clicked.append(self.selector)

        def wait_for(self, state=None, timeout=None):
            pass

    class MockPage:
        def locator(self, selector):
            if "span.v-icon.IcoMoon-Ultimate" in selector:
                return MockLocator(selector, exists=True, visible=True)
            if "span.v-menubar-menuitem-caption" in selector:
                return MockLocator(selector, exists=True, visible=True)
            if "Download" in selector:
                return MockLocator(selector, exists=True, visible=True)
            if "Close" in selector:
                return MockLocator(selector, exists=True, visible=True)
            return MockLocator(selector, exists=False, visible=False)

        def wait_for_timeout(self, ms):
            pass

        def expect_download(self, timeout=None):
            return MockExpectDownload()

    page = MockPage()
    res = _sort_and_export(page, timeout=30000)

    assert res is not None
    assert res.exists()
    assert res.name == "usage_export.xlsx"
    assert any("IcoMoon-Ultimate" in c for c in clicked)
    assert any("Download" in c for c in clicked)
    assert any("Close" in c for c in clicked)


def test_login_cmp_normalizes_plain_base_url():
    from network_report.collectors.cmp_crawler import _SERVICE_LOGIN_URL, _login_cmp

    navigated_urls = []

    class MockLocator:
        @property
        def first(self):
            return self

        def click(self, *args, **kwargs):
            pass

        def count(self):
            return 0

    class MockPage:
        url = "https://ep.iotcc.telkomsel.com/cas/login"

        def goto(self, url, *args, **kwargs):
            navigated_urls.append(url)

        def wait_for_selector(self, *args, **kwargs):
            pass

        def fill(self, *args, **kwargs):
            pass

        def locator(self, *args, **kwargs):
            return MockLocator()

        def wait_for_timeout(self, *args, **kwargs):
            pass

    page = MockPage()
    _login_cmp(page, "https://ep.iotcc.telkomsel.com", "user", "pass", timeout=10000)
    assert navigated_urls[0] == _SERVICE_LOGIN_URL


def test_sort_and_export_with_menubar_fallback():
    from network_report.collectors.cmp_crawler import _sort_and_export

    clicked = []

    class MockDownload:
        suggested_filename = "menubar_export.xlsx"

        def save_as(self, path):
            Path(path).write_text("data")

    class MockDownloadInfo:
        value = MockDownload()

    class MockExpectDownload:
        def __enter__(self):
            return MockDownloadInfo()

        def __exit__(self, *args):
            pass

    class MockLocator:
        def __init__(self, selector, exists=True, visible=True):
            self.selector = selector
            self._exists = exists
            self._visible = visible

        @property
        def first(self):
            return self

        def locator(self, sub_sel):
            return MockLocator(f"{self.selector} >> {sub_sel}", False, False)

        def filter(self, *args, **kwargs):
            return self

        def count(self):
            return 1 if self._exists else 0

        def is_visible(self):
            return self._visible

        def click(self, timeout=None, force=False):
            clicked.append(self.selector)

        def wait_for(self, state=None, timeout=None):
            pass

    class MockPage:
        def locator(self, selector):
            # No IcoMoon icon, but menubar item exists
            if "IcoMoon-Ultimate" in selector:
                return MockLocator(selector, exists=False, visible=False)
            if ".v-menubar-menuitem:has-text('Export')" in selector:
                return MockLocator(selector, exists=True, visible=True)
            if "span.v-menubar-menuitem-caption" in selector:
                return MockLocator(selector, exists=True, visible=True)
            if "Download" in selector:
                return MockLocator(selector, exists=True, visible=True)
            return MockLocator(selector, exists=False, visible=False)

        def wait_for_timeout(self, ms):
            pass

        def expect_download(self, timeout=None):
            return MockExpectDownload()

    page = MockPage()
    res = _sort_and_export(page, timeout=30000)
    assert res is not None
    assert any("v-menubar-menuitem" in c for c in clicked)
    assert any("Download" in c for c in clicked)


def test_cmp_export_timeout_setting_from_env_and_default(monkeypatch):
    from network_report.config import CmpSettings, load_config

    # Default is 300 seconds
    monkeypatch.delenv("CMP_EXPORT_TIMEOUT_SECONDS", raising=False)
    cfg_default = load_config()
    assert cfg_default.cmp.export_timeout_seconds == 300

    cmp_direct = CmpSettings()
    assert cmp_direct.export_timeout_seconds == 300

    # Read from environment
    monkeypatch.setenv("CMP_EXPORT_TIMEOUT_SECONDS", "480")
    cfg_custom = load_config()
    assert cfg_custom.cmp.export_timeout_seconds == 480


def test_cmp_monthly_export_setting_and_skip_behavior(monkeypatch, tmp_path, caplog):
    """Monthly export is skipped by default/when disabled, and attempted when enabled."""
    import logging

    from network_report.collectors.cmp_crawler import run_cmp_crawler
    from network_report.config import CmpSettings, load_config

    monkeypatch.delenv("CMP_MONTHLY_EXPORT_ENABLED", raising=False)
    assert CmpSettings().monthly_export_enabled is False
    assert load_config().cmp.monthly_export_enabled is False

    for val in ("true", "1", "yes", "True", "YES"):
        monkeypatch.setenv("CMP_MONTHLY_EXPORT_ENABLED", val)
        assert load_config().cmp.monthly_export_enabled is True

    for val in ("false", "0", "no", "anything_else"):
        monkeypatch.setenv("CMP_MONTHLY_EXPORT_ENABLED", val)
        assert load_config().cmp.monthly_export_enabled is False

    class FakeBrowser:
        def new_context(self, *a, **kw):
            return self

        def new_page(self):
            return FakePage()

        def set_default_timeout(self, *a):
            pass

        def close(self):
            pass

    class FakePlaywright:
        class firefox:
            @staticmethod
            def launch(**kwargs):
                return FakeBrowser()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("network_report.collectors.cmp_crawler.sync_playwright", FakePlaywright)
    monkeypatch.setattr("network_report.collectors.cmp_crawler._login_cmp", lambda *a, **kw: True)
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._navigate_to_dashboard", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._save_dashboard_screenshot_and_sidecar",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._handle_2fa_if_needed", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._navigate_to_usage_query", lambda *a, **kw: True
    )

    set_report_calls = []

    def mock_set_report_type(page, report_type, target, timeout):
        set_report_calls.append((report_type, target))
        return True

    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._set_report_type_and_date", mock_set_report_type
    )

    def mock_sort_and_export(page, timeout, **kwargs):
        t = tmp_path / f"export_{len(set_report_calls)}.xlsx"
        t.write_text("dummy")
        return t

    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler._sort_and_export", mock_sort_and_export
    )

    # When flag is OFF:
    monkeypatch.setenv("CMP_MONTHLY_EXPORT_ENABLED", "false")
    set_report_calls.clear()
    with caplog.at_level(logging.INFO):
        res_off = run_cmp_crawler(
            base_url="https://ep.iotcc.telkomsel.com",
            username="user",
            password="pwd",
            target_date="2026-10-12",
            target_month="2026-10",
            output_dir=tmp_path,
            headless=True,
        )

    assert [c[0] for c in set_report_calls] == ["Daily"]
    assert res_off.daily_file is not None
    assert res_off.monthly_file is None
    assert (
        "CMP monthly export is disabled; month-to-date figure will be summed from the daily exports"
        in caplog.text
    )

    # When flag is ON:
    set_report_calls.clear()
    res_on = run_cmp_crawler(
        base_url="https://ep.iotcc.telkomsel.com",
        username="user",
        password="pwd",
        target_date="2026-10-12",
        target_month="2026-10",
        output_dir=tmp_path,
        headless=True,
        monthly_export_enabled=True,
    )

    assert [c[0] for c in set_report_calls] == ["Daily", "Monthly"]
    assert res_on.daily_file is not None
    assert res_on.monthly_file is not None
