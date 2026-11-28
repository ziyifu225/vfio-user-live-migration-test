#!/usr/bin/env python3
"""Same-host migration of a required real USB device through usb-host."""
import argparse
from contextlib import ExitStack
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shlex
import socket
import subprocess
import sys
import tempfile
import time

from hardware.experiment_paths import create_output_directory

REVISION = "005c61c0ef6f6ae615283440b4bb7be51b2c6ddd"
MACHINE = "pc-q35-11.1"
SYS_USB = Path("/sys/bus/usb/devices")
TRACE_EVENTS = """usb_host_*
usb_port_*
migrate_set_state
vmstate_save
vmstate_load
vmstate_load_state
qemu_loadvm_state_section_startfull
"""
USB_LINE = re.compile(
    r"EXPERIMENT_USB boot=(\S+) seq=(\d+) vid=([0-9a-f]{4}) "
    r"pid=([0-9a-f]{4}) devnum=(\d+) config=(\d+)"
)


def read(path):
    try:
        return Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return ""


def migration_state(value):
    """Keep status and failure reasons only."""
    return {key: value[key] for key in ("status", "error-desc", "blocked-reasons") if key in value}


def format_record(record):
    if "phase" in record:
        extra = {key: value for key, value in record.items()
                 if key not in ("sequence", "utc", "role", "phase")}
        body = record["phase"] + (" " + json.dumps(extra, ensure_ascii=False) if extra else "")
    else:
        body = record.get("direction", "") + " " + json.dumps(record.get("message", {}), ensure_ascii=False)
    return f'[{record["sequence"]}] [{record["utc"]}] [{record.get("role", "controller")}] {body}\n'


def write_logs(output, outcome, journal=None, raw=None):
    """Keep only the verdict, both peers' logs, and the migration operation log."""
    records = journal.records if journal else []
    if not (output / "migration.log").exists():
        (output / "migration.log").write_text("")
    for role in ("source", "destination"):
        sections = ["=== QMP 命令、状态与事件 ===\n" + "".join(
            format_record(record) for record in records if record.get("role") == role)]
        for title, suffix in (("USB 与迁移 trace", "trace.log"),
                              ("QEMU / libusb 诊断", "stderr.log"),
                              ("guest 内核与 USB 枚举", "serial.log")):
            sections.append(f"=== {title} ===\n" + (read(raw / f"{role}.{suffix}") if raw else ""))
        (output / f"{role}.log").write_text("\n".join(sections))

    lines = [f'实验结果：{outcome.get("result", "FAIL")}',
             '虚拟机迁移：' + ("成功" if outcome.get("vm_migration_passed") else "未完成"),
             '物理 USB 重新枚举：' + ("成功" if outcome.get("usb_reenumeration_passed") else "未完成")]
    for role in ("source", "destination"):
        status = outcome.get(f"{role}_status", {}).get("status", "未确认")
        lines.append(f"{role} 迁移后状态：{status}")
    for key in ("error", "cleanup_error"):
        if outcome.get(key):
            lines.append(f'失败原因：{outcome[key]}')
    lines += [f"source 日志：{output / 'source.log'}",
              f"destination 日志：{output / 'destination.log'}",
              f"迁移操作顺序：{output / 'migration.log'}"]
    result = "\n".join(lines) + "\n"
    (output / "result.log").write_text(result)
    return result


def devices(root=SYS_USB):
    found = []
    for path in sorted(root.iterdir()):
        if not re.fullmatch(r"\d+-\d+(?:\.\d+)*", path.name):
            continue
        if not (path / "idVendor").exists():
            continue
        bus, address = int(read(path / "busnum")), int(read(path / "devnum"))
        found.append({
            "port": path.name, "bus": bus, "address": address,
            "hostport": path.name.split("-", 1)[1],
            "vid": read(path / "idVendor").strip().lower(),
            "pid": read(path / "idProduct").strip().lower(),
            "serial": read(path / "serial").strip(),
            "product": read(path / "product").strip(),
            "class": read(path / "bDeviceClass").strip(),
            "sysfs": str(path.resolve()),
            "node": f"/dev/bus/usb/{bus:03d}/{address:03d}",
        })
    return found


def select_device(args, available):
    if args.usb:
        matches = [d for d in available if d["port"] == args.usb]
    elif args.usb_id:
        matches = [d for d in available if f'{d["vid"]}:{d["pid"]}' == args.usb_id.lower()]
    else:
        bus, address = map(int, args.usb_address.split(":"))
        matches = [d for d in available if (d["bus"], d["address"]) == (bus, address)]
    if len(matches) != 1:
        raise RuntimeError(f"USB selector matched {len(matches)} devices; use --list and an exact --usb PORT")
    device = matches[0]
    if device["class"] == "09":
        raise RuntimeError("USB hubs cannot be used as the experiment device")
    return device


def check_device(device):
    node = device["node"]
    if not os.access(node, os.R_OK | os.W_OK):
        raise RuntimeError(f"Need read/write access to {node}; see README udev rule")
    # Refuse mounted USB storage, including partitions and stacked dm devices.
    mounted = set()
    for line in read("/proc/self/mountinfo").splitlines():
        fields = line.split()
        if len(fields) > 2:
            mounted.add(fields[2])
    swaps = {str(Path(line.split()[0]).resolve()) for line in read("/proc/swaps").splitlines()[1:]}
    usb_path = Path(device["sysfs"])
    block_root = Path("/sys/class/block")
    descendants = {p.name for p in block_root.iterdir() if p.resolve().is_relative_to(usb_path)}
    pending = list(descendants)
    while pending:
        name = pending.pop()
        for holder in (block_root / name / "holders").iterdir():
            if holder.name not in descendants:
                descendants.add(holder.name)
                pending.append(holder.name)
    for name in descendants:
        if read(block_root / name / "dev").strip() in mounted or f"/dev/{name}" in swaps:
            raise RuntimeError(f"{name} on selected USB device is mounted or used as swap")


def usb_observations(text, vid, pid):
    return [
        {"boot": m[1], "seq": int(m[2]), "vid": m[3], "pid": m[4],
         "devnum": int(m[5]), "config": int(m[6])}
        for m in USB_LINE.finditer(text)
        if m[3] == vid and m[4] == pid and int(m[6]) > 0
    ]


class Journal:
    def __init__(self, path):
        self.file = path.open("w", buffering=1)
        self.sequence = 0
        self.records = []

    def write(self, **data):
        self.sequence += 1
        record = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "sequence": self.sequence, **data}
        self.records.append(record)
        self.file.write(format_record(record))


class QMP:
    def __init__(self, path, role, journal, timeout):
        self.role, self.journal, self.sequence = role, journal, 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rb")
        greeting = self.receive()
        if "QMP" not in greeting:
            raise RuntimeError(f"{role}: invalid QMP greeting")
        self.command("qmp_capabilities")

    def receive(self, execute=None):
        line = self.file.readline()
        if not line:
            raise RuntimeError(f"{self.role}: QMP disconnected")
        value = json.loads(line)
        if execute == "query-migrate" and "return" in value:
            value["return"] = migration_state(value["return"])
        self.journal.write(role=self.role, direction="receive", message=value)
        return value

    def command(self, execute, arguments=None):
        self.sequence += 1
        request = {"execute": execute, "id": self.sequence}
        if arguments is not None:
            request["arguments"] = arguments
        self.journal.write(role=self.role, direction="send", message=request)
        self.sock.sendall(json.dumps(request).encode() + b"\n")
        deadline = time.monotonic() + self.sock.gettimeout()
        while time.monotonic() < deadline:
            response = self.receive(execute)
            if response.get("id") == self.sequence:
                if "error" in response:
                    raise RuntimeError(f'{self.role}: {execute}: {response["error"]}')
                return response["return"]
        raise TimeoutError(f"{self.role}: QMP {execute} timed out")

    def close(self):
        self.file.close()
        self.sock.close()


class Experiment:
    def __init__(self, args, output, journal, qemu, guest, device, runtime):
        self.args, self.output, self.journal = args, output, journal
        self.qemu, self.guest, self.device, self.runtime = qemu, guest, device, runtime
        self.processes, self.monitors, self.handles = {}, {}, []
        self.findings = {"vm_migration_passed": False, "usb_reenumeration_passed": False}

    def wait(self, description, predicate, timeout=None):
        deadline = time.monotonic() + (timeout or self.args.timeout)
        while time.monotonic() < deadline:
            for role, proc in self.processes.items():
                if proc.poll() is not None:
                    raise RuntimeError(f"{role} exited ({proc.returncode}); see {role}.log")
            value = predicate()
            if value:
                self.journal.write(phase=description)
                return value
            time.sleep(0.15)
        raise TimeoutError(f"Timed out: {description}")

    def launch(self, role):
        socket_path = self.runtime / f"{role}.qmp"
        trace = self.runtime / f"{role}.trace.log"
        args = [self.qemu, "-name", f"usb-migration-{role}",
                "-machine", MACHINE, "-accel", self.args.accel,
                "-cpu", "max" if self.args.accel == "tcg" else "host",
                "-m", "256", "-smp", "1", "-nodefaults", "-no-user-config",
                "-display", "none", "-monitor", "none", "-nic", "none",
                "-no-reboot", "-kernel", str(self.guest / "kernel"),
                "-initrd", str(self.guest / "initrd"),
                "-append", "console=ttyS0,115200 panic=-1 rdinit=/init nokaslr",
                "-serial", f"file:{self.runtime / (role + '.serial.log')}",
                "-qmp", f"unix:{socket_path},server=on,wait=off",
                "-D", str(trace), "-trace", f"events={self.runtime / 'trace-events'}",
                "-device", "qemu-xhci,id=xhci,bus=pcie.0,addr=0x5"]
        topology = "id=physical,bus=xhci.0,port=1"
        d = self.device
        args += ["-device", f'usb-host,{topology},hostbus={d["bus"]},'
                 f'hostport={d["hostport"]},vendorid=0x{d["vid"]},productid=0x{d["pid"]}']
        if role == "destination":
            # Keep destination paused through load: no autoscan/claim before source exits.
            args += ["-S", "-incoming", "defer"]
        self.journal.write(role=role, phase="启动命令：" + shlex.join(args))
        stderr = (self.runtime / f"{role}.stderr.log").open("wb")
        self.handles.append(stderr)
        self.processes[role] = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr)
        self.journal.write(phase="launch", role=role, pid=self.processes[role].pid)
        self.wait(f"{role} QMP available", socket_path.exists)
        self.monitors[role] = QMP(socket_path, role, self.journal, min(10, self.args.timeout))
        version = self.monitors[role].command("query-version")["qemu"]
        if version != {"major": 11, "minor": 1, "micro": 3}:
            raise RuntimeError(f"Wrong QEMU version: {version}")
        return self.monitors[role]

    def observations(self, role):
        d = self.device
        return usb_observations(read(self.runtime / f"{role}.serial.log"), d["vid"], d["pid"])

    def snapshot(self, role, stage):
        monitor = self.monitors[role]
        data = {"status": monitor.command("query-status"),
                "migration": monitor.command("query-migrate"),
                "usb": monitor.command("human-monitor-command", {"command-line": "info usb"})}
        self.journal.write(role=role, phase=f"状态 ({stage})", state=data)
        return data

    def stop_source(self):
        proc = self.processes["source"]
        try:
            self.monitors["source"].command("quit")
        except RuntimeError:
            if proc.poll() is None:
                proc.wait(timeout=10)
        proc.wait(timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(f"Source exited with {proc.returncode} during USB handoff")
        self.monitors.pop("source").close()
        del self.processes["source"]
        self.journal.write(phase="source exited; physical device released")

    def run(self):
        source = self.launch("source")
        before = self.wait("source guest configured USB device", lambda: self.observations("source"))[-1]
        source_trace = read(self.runtime / "source.trace.log")
        source_evidence = {name: name in source_trace for name in (
            "usb_host_open_success", "usb_host_set_config"
        )}
        if not all(source_evidence.values()):
            raise RuntimeError(f"Missing source physical USB evidence: {source_evidence}")
        self.snapshot("source", "before")
        destination = self.launch("destination")
        # Migration must not race with an active physical handle on destination.
        if "usb_host_open_success" in read(self.runtime / "destination.trace.log"):
            raise RuntimeError("Destination claimed a physical USB device before handoff")
        migration_uri = f"unix:{self.runtime / 'migration.sock'}"
        destination.command("migrate-incoming", {"uri": migration_uri})
        self.wait("migration listener ready", lambda: (self.runtime / "migration.sock").exists())
        source.command("migrate", {"uri": migration_uri})

        def completed():
            state = source.command("query-migrate")
            if state.get("status") in ("failed", "cancelled"):
                raise RuntimeError(f"Migration {state}")
            return state if state.get("status") == "completed" else None

        self.wait("VM migration completed", completed)
        source_final = self.snapshot("source", "after-migration")
        if source_final["status"]["status"] != "postmigrate":
            raise RuntimeError(f"Unexpected source state: {source_final['status']}")
        self.wait("destination received migration", lambda:
                  destination.command("query-migrate").get("status") == "completed")
        self.findings.update(source_status=source_final["status"],
                             vm_migration_passed=True)
        dest_paused = self.snapshot("destination", "before-handoff")
        if dest_paused["status"]["running"]:
            raise RuntimeError("Destination unexpectedly running before exclusive USB handoff")
        if "usb_host_open_success" in read(self.runtime / "destination.trace.log"):
            raise RuntimeError("Destination opened USB before source released it")
        self.stop_source()
        destination.command("cont")
        after = self.wait("destination guest configured USB device", lambda: [
            obs for obs in self.observations("destination")
            if obs["boot"] == before["boot"] and obs["seq"] > before["seq"]
            and obs["devnum"] != before["devnum"]
        ])[-1]
        self.wait("destination guest continues running", lambda: [
            obs for obs in self.observations("destination")
            if obs["boot"] == after["boot"] and obs["seq"] >= after["seq"] + 2
        ])
        final = self.snapshot("destination", "after")
        self.findings["destination_status"] = final["status"]
        if not final["status"]["running"]:
            raise RuntimeError("Destination VM is not running")
        trace = read(self.runtime / "destination.trace.log")
        evidence = {name: name in trace for name in (
            "usb_host_open_success", "usb_port_detach", "usb_port_attach", "usb_host_set_config"
        )}
        if not all(evidence.values()):
            raise RuntimeError(f"Missing USB reconnect trace evidence: {evidence}")
        self.findings["usb_reenumeration_passed"] = True
        return self.findings

    def cleanup(self):
        # Stop destination first; never resume a stopped migration source.
        for role in ("destination", "source"):
            proc = self.processes.get(role)
            if proc is None:
                continue
            if proc.poll() is None:
                try:
                    self.monitors[role].command("quit")
                except (KeyError, OSError, RuntimeError, TimeoutError):
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            self.journal.write(phase="cleanup", role=role, exit_code=proc.returncode)
        for monitor in self.monitors.values():
            monitor.close()
        for handle in self.handles:
            handle.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="list host USB ports and IDs; no device access")
    mode.add_argument("--usb", metavar="PORT", help="exact sysfs USB port, e.g. 1-2.3")
    mode.add_argument("--usb-id", metavar="VID:PID", help="unique USB vendor/product pair, e.g. 046a:b090")
    mode.add_argument("--usb-address", metavar="BUS:ADDR", help="lsusb bus/address, e.g. 001:005; converted to stable port")
    p.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
    p.add_argument("--timeout", type=float, default=120, help="seconds per phase (default 120)")
    p.add_argument("--output", type=Path, help="new log directory relative to the experiment directory")
    return p


def main():
    p = parser()
    args = p.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        p.error("--timeout must be finite and positive")
    if args.usb_id and not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", args.usb_id):
        p.error("--usb-id must have format 046a:b090")
    if args.usb_address and not re.fullmatch(r"\d{1,3}:\d{1,3}", args.usb_address):
        p.error("--usb-address must have format 001:005")
    if args.list:
        for d in devices():
            print(f'{d["port"]:12} {d["bus"]:03d}:{d["address"]:03d} '
                  f'{d["vid"]}:{d["pid"]} {d["product"]} serial={d["serial"]}')
        return 0

    output = journal = runtime = experiment = None
    outcome = {"result": "FAIL", "vm_migration_passed": False, "usb_reenumeration_passed": False}
    with ExitStack() as stack:
        try:
            output = create_output_directory(args.output)
            journal = Journal(output / "migration.log")
            stack.callback(journal.file.close)
            qemu = os.environ["EXPERIMENT_QEMU"]
            guest = Path(os.environ["EXPERIMENT_GUEST"])
            if not Path(qemu).is_file() or not all((guest / f).is_file() for f in ("kernel", "initrd")):
                raise RuntimeError("Missing QEMU/guest build outputs; use nix run .#qemu-physical-device")
            revision_file = Path(qemu).parent.parent / "share/qemu/experiment-revision"
            if read(revision_file).strip() != REVISION:
                raise RuntimeError("QEMU build revision does not match this experiment")
            if args.accel == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
                raise RuntimeError("Need read/write access to /dev/kvm; alternatively use --accel tcg")
            device = select_device(args, devices())
            check_device(device)
            journal.write(phase=f"QEMU 11.1.3 commit={REVISION}; USB=" + json.dumps(device, ensure_ascii=False))
            lock_name = hashlib.sha256(device["port"].encode()).hexdigest()[:16]
            lock_root = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
            lock = stack.enter_context((lock_root / f"qemu-usb-{os.getuid()}-{lock_name}.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another experiment is already using this USB port") from exc
            runtime = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="qpd-")))
            (runtime / "trace-events").write_text(TRACE_EVENTS)
            experiment = Experiment(args, output, journal, qemu, guest, device, runtime)
            outcome.update(experiment.run())
            outcome["result"] = "PASS"
        except (Exception, KeyboardInterrupt) as exc:
            outcome["error"] = str(exc) or type(exc).__name__
            if journal:
                journal.write(phase="failure", error=outcome["error"])
        finally:
            if experiment:
                outcome.update(experiment.findings)
                try:
                    experiment.cleanup()
                except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
                    outcome.update(result="FAIL", cleanup_error=str(exc))
            if output and journal:
                print(write_logs(output, outcome, journal, runtime), end="", flush=True)
            elif outcome.get("error"):
                print(f'ERROR: {outcome["error"]}', file=sys.stderr)
    return 0 if outcome["result"] == "PASS" else 1


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
