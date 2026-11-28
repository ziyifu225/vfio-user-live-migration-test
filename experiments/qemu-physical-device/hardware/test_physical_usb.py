"""Run a real USB migration and verify its logs. No extra reports are written."""
import os
import re
import shlex
import shutil
import subprocess


LOG_FILES = ("result.log", "source.log", "destination.log", "migration.log")
USB_LINE = re.compile(
    r"EXPERIMENT_USB boot=(\S+) seq=(\d+) vid=([0-9a-f]{4}) "
    r"pid=([0-9a-f]{4}) devnum=(\d+) config=(\d+)"
)


def test_same_host_real_usb_migration(hardware_configuration):
    output, arguments = hardware_configuration
    runner = os.environ.get("EXPERIMENT_RUNNER") or shutil.which("qemu-physical-device")
    assert runner, "Run via nix run .#qemu-physical-device-test or nix develop"
    pending = output / ".pending"
    completed = subprocess.run(
        [runner, *arguments, "--output", str(pending)],
        text=True, capture_output=True, check=False,
    )
    for name in LOG_FILES:
        if (pending / name).exists():
            shutil.move(pending / name, output / name)
    if pending.exists():
        pending.rmdir()
    result_path = output / "result.log"
    result_path.write_text(result_path.read_text().replace(str(pending), str(output)))
    if completed.returncode != 0:
        if not result_path.read_text().startswith("实验结果：FAIL"):
            result_path.write_text(f"实验结果：FAIL\n{completed.stdout}\n{completed.stderr}")
        raise AssertionError("真实 USB 迁移失败，原因见 result.log")

    logs = {name: (output / name).read_text() for name in LOG_FILES}
    for verdict in ("实验结果：PASS", "虚拟机迁移：成功", "物理 USB 重新枚举：成功"):
        assert verdict in logs["result.log"]
    migration = logs["migration.log"]
    assert "005c61c0ef6f6ae615283440b4bb7be51b2c6ddd" in migration
    assert '"status": "completed"' in logs["source.log"]
    assert '"status": "postmigrate"' in logs["source.log"]
    assert '"status": "running"' in logs["destination.log"]

    selectors = []
    for role, events in (
        ("source", ("usb_host_open_success", "usb_host_set_config")),
        ("destination", ("usb_host_open_success", "usb_port_detach",
                         "usb_port_attach", "usb_host_set_config")),
    ):
        text = logs[f"{role}.log"]
        command = next(line.split("启动命令：", 1)[1] for line in text.splitlines() if "启动命令：" in line)
        argv = shlex.split(command)
        devices = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-device"]
        passthrough = [device for device in devices if device.startswith("usb-host,")]
        assert len(passthrough) == 1, f"{role} must use real usb-host passthrough"
        fields = dict(field.split("=", 1) for field in passthrough[0].split(",")[1:])
        selectors.append({key: fields[key] for key in ("hostbus", "hostport", "vendorid", "productid")})
        assert all(event in text for event in events), f"Missing {role} physical USB evidence"
    assert selectors[0] == selectors[1], "both peers must select the same real USB device"
    vid = f'{int(selectors[0]["vendorid"], 16):04x}'
    pid = f'{int(selectors[0]["productid"], 16):04x}'

    def observations(role):
        return [(m[1], int(m[2]), int(m[5])) for m in USB_LINE.finditer(logs[f"{role}.log"])
                if m[3] == vid and m[4] == pid and int(m[6]) > 0]

    before = observations("source")
    assert before, "source guest must report the selected, configured USB"
    after = [entry for entry in observations("destination")
             if entry[0] == before[0][0] and entry[1] > before[0][1]
             and entry[2] != before[0][2]]
    assert after, "destination must re-enumerate USB in the same guest without rebooting"
    assert after[-1][1] >= after[0][1] + 2, "guest must continue running after reconnect"

    lines = migration.splitlines()
    released = next(i for i, line in enumerate(lines) if "source exited; physical device released" in line)
    resumed = next(i for i, line in enumerate(lines) if "[destination]" in line and '"execute": "cont"' in line)
    assert released < resumed, "source must release USB before destination resumes"
    assert any("[destination] cleanup" in line and '"exit_code": 0' in line for line in lines)
    assert {path.name for path in output.iterdir()} == set(LOG_FILES)
