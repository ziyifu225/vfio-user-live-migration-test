"""Runtime configuration for the real-device hardware integration test."""
import math
from pathlib import Path
import sys

import pytest

from experiment_paths import create_output_directory


def pytest_addoption(parser):
    group = parser.getgroup("physical USB migration")
    group.addoption("--usb", metavar="PORT", help="real host USB port, e.g. 1-2.3")
    group.addoption("--usb-id", metavar="VID:PID", help="unique real USB VID:PID")
    group.addoption("--usb-address", metavar="BUS:ADDR", help="lsusb bus/address, e.g. 001:005")
    group.addoption("--accel", choices=("kvm", "tcg"), default="kvm")
    group.addoption("--timeout", type=float, default=120, help="seconds per migration phase")
    group.addoption("--output", type=Path, help="new log directory relative to the experiment directory")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if config.option.collectonly or config.option.help:
        return
    selectors = [config.getoption(name) for name in ("usb", "usb_id", "usb_address")]
    if sum(bool(value) for value in selectors) != 1:
        raise pytest.UsageError(
            "A real USB device is required: specify exactly one of "
            "--usb PORT, --usb-id VID:PID, --usb-address BUS:ADDR."
        )
    timeout = config.getoption("timeout")
    if not math.isfinite(timeout) or timeout <= 0:
        raise pytest.UsageError("--timeout must be finite and positive")
    try:
        output = create_output_directory(config.getoption("output"))
    except (OSError, RuntimeError) as exc:
        raise pytest.UsageError(f"Cannot create a new result directory: {exc}") from exc
    config.physical_usb_output = output
    for name, text in (
        ("result.log", "硬件测试尚未执行\n"),
        ("source.log", "source 尚未启动\n"),
        ("destination.log", "destination 尚未启动\n"),
        ("migration.log", "迁移尚未开始\n"),
    ):
        (output / name).write_text(text)


def pytest_report_header(config):
    output = getattr(config, "physical_usb_output", None)
    return f"REAL USB hardware test; results: {output}" if output else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        output = item.config.physical_usb_output
        path = output / "result.log"
        previous = path.read_text() if path.exists() else ""
        if previous.startswith("实验结果：FAIL"):
            return
        previous = "\n".join(line for line in previous.splitlines()
                             if not line.startswith("实验结果："))
        reason = str(call.excinfo.value) if call.excinfo else report.longreprtext
        path.write_text("实验结果：FAIL\n检查失败：" + reason + "\n" + previous)


def pytest_sessionfinish(session, exitstatus):
    output = getattr(session.config, "physical_usb_output", None)
    if output is None:
        return
    result = output / "result.log"
    if not result.exists():
        result.write_text(f"硬件测试未完成，pytest 退出码：{exitstatus}\n")
    sys.__stdout__.write(result.read_text().rstrip() + f"\n日志目录：{output}\n")
    sys.__stdout__.flush()


@pytest.fixture(scope="session")
def hardware_configuration(request):
    config = request.config
    arguments = []
    for option in ("usb", "usb_id", "usb_address"):
        value = config.getoption(option)
        if value:
            arguments += ["--" + option.replace("_", "-"), value]
    arguments += ["--accel", config.getoption("accel"),
                  "--timeout", str(config.getoption("timeout"))]
    return config.physical_usb_output, arguments
