import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import os
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run
from hardware import experiment_paths


class OutputPathTests(unittest.TestCase):
    def test_default_results_stay_in_experiment_when_cwd_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = Path(directory) / "experiment"
            outside = Path(directory) / "outside"
            outside.mkdir()
            with patch.dict(os.environ, {"EXPERIMENT_DIRECTORY": str(experiment)}), \
                    patch.object(Path, "cwd", return_value=outside):
                output = experiment_paths.create_output_directory()
            self.assertEqual(output.parent, experiment / "results")
            self.assertFalse((outside / "results").exists())

    def test_custom_output_is_relative_to_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = Path(directory) / "experiment"
            with patch.dict(os.environ, {"EXPERIMENT_DIRECTORY": str(experiment)}):
                output = experiment_paths.create_output_directory("results/named-run")
            self.assertEqual(output, experiment / "results/named-run")

    def test_output_cannot_escape_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = Path(directory) / "experiment"
            with patch.dict(os.environ, {"EXPERIMENT_DIRECTORY": str(experiment)}):
                with self.assertRaisesRegex(RuntimeError, "inside the experiment"):
                    experiment_paths.create_output_directory("../outside")
            self.assertFalse((Path(directory) / "outside").exists())

    def test_packaged_code_finds_checkout_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            experiment = root / "experiments/qemu-physical-device"
            experiment.mkdir(parents=True)
            (experiment / "run.py").touch()
            nested = root / "nested/working-directory"
            nested.mkdir(parents=True)
            is_file = Path.is_file

            def packaged_marker(path):
                return path == Path("/nix/store/example-runner-source/run.py") or is_file(path)

            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(experiment_paths, "__file__", "/nix/store/example-runner-source/hardware/experiment_paths.py"), \
                    patch.object(Path, "is_file", packaged_marker), \
                    patch.object(Path, "cwd", return_value=nested):
                self.assertEqual(experiment_paths.experiment_directory(), experiment)


class SelectDeviceTests(unittest.TestCase):
    def setUp(self):
        self.devices = [
            {"port": "1-2", "vid": "046a", "pid": "b090", "bus": 1, "address": 5, "class": "00"},
            {"port": "1-3", "vid": "046a", "pid": "b090", "bus": 1, "address": 6, "class": "00"},
        ]

    def args(self, **values):
        return argparse.Namespace(**({"usb": None, "usb_id": None, "usb_address": None} | values))

    def test_duplicate_vid_pid_requires_exact_port(self):
        with self.assertRaisesRegex(RuntimeError, "matched 2 devices"):
            run.select_device(self.args(usb_id="046a:b090"), self.devices)
        self.assertEqual(run.select_device(self.args(usb="1-3"), self.devices)["address"], 6)

    def test_lsusb_address_resolves_to_stable_port(self):
        self.assertEqual(run.select_device(self.args(usb_address="001:005"), self.devices)["port"], "1-2")

    def test_hubs_rejected(self):
        self.devices[0]["class"] = "09"
        with self.assertRaisesRegex(RuntimeError, "hubs"):
            run.select_device(self.args(usb="1-2"), self.devices)

    def test_missing_device_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "matched 0 devices"):
            run.select_device(self.args(usb="2-1"), self.devices)


class EvidenceTests(unittest.TestCase):
    def test_only_matching_configured_usb_counts(self):
        lines = "\n".join([
            "EXPERIMENT_USB boot=abc seq=5 vid=046a pid=b090 devnum=2 config=1",
            "EXPERIMENT_USB boot=abc seq=6 vid=046a pid=b090 devnum=3 config=0",
            "EXPERIMENT_USB boot=abc seq=7 vid=0627 pid=0001 devnum=4 config=1",
            "EXPERIMENT_USB boot=abc seq=8 vid=046a pid=b090 devnum=5 config=1",
        ])
        observations = run.usb_observations(lines, "046a", "b090")
        self.assertEqual([o["seq"] for o in observations], [5, 8])
        self.assertEqual(observations[-1]["devnum"], 5)


class QMPTests(unittest.TestCase):
    def exercise(self, responses):
        client, server = socket.socketpair()
        client.settimeout(2)
        journal_file = tempfile.TemporaryDirectory()
        self.addCleanup(journal_file.cleanup)
        journal = run.Journal(Path(journal_file.name) / "qmp.log")
        self.addCleanup(journal.file.close)
        qmp = run.QMP.__new__(run.QMP)
        qmp.role, qmp.journal, qmp.sequence = "source", journal, 0
        qmp.sock, qmp.file = client, client.makefile("rb")
        self.addCleanup(qmp.close)

        def respond():
            with server, server.makefile("rb") as file:
                request = json.loads(file.readline())
                for response in responses:
                    response = dict(response)
                    if "event" not in response:
                        response["id"] = request["id"]
                    server.sendall(json.dumps(response).encode() + b"\n")
        thread = threading.Thread(target=respond)
        thread.start()
        self.addCleanup(thread.join, 3)
        return qmp, journal

    def test_async_events_do_not_replace_command_response(self):
        qmp, journal = self.exercise([
            {"event": "MIGRATION", "data": {"status": "completed"}},
            {"return": {"status": "completed"}},
        ])
        self.assertEqual(qmp.command("query-migrate"), {"status": "completed"})
        records = journal.records
        self.assertEqual(len(records), 3)
        self.assertEqual(records[1]["message"]["event"], "MIGRATION")

    def test_qmp_error_is_not_a_pass(self):
        qmp, _ = self.exercise([{"error": {"class": "GenericError", "desc": "blocked"}}])
        with self.assertRaisesRegex(RuntimeError, "blocked"):
            qmp.command("migrate", {"uri": "unix:/unused"})

    def test_migration_metrics_are_not_collected(self):
        qmp, journal = self.exercise([{"return": {
            "status": "completed", "total-time": 125, "downtime": 12,
            "ram": {"mbps": 1000, "transferred": 4096},
        }}])
        self.assertEqual(qmp.command("query-migrate"), {"status": "completed"})
        recorded = journal.records
        self.assertEqual(recorded[-1]["message"]["return"], {"status": "completed"})
        self.assertNotIn("elapsed", recorded[-1])
        self.assertNotIn("total-time", Path(journal.file.name).read_text())

    def test_eof_is_not_a_pass(self):
        qmp, _ = self.exercise([])
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            qmp.command("query-status")


class LogTests(unittest.TestCase):
    def test_logs_show_both_peers_actions_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "logs"
            raw = Path(directory) / "raw"
            output.mkdir()
            raw.mkdir()
            journal = run.Journal(output / "migration.log")
            journal.write(role="source", phase="source exited; physical device released")
            journal.write(role="destination", direction="send", message={"execute": "cont"})
            journal.file.close()
            (raw / "source.trace.log").write_text("usb_host_release_interface\n")
            (raw / "source.stderr.log").write_text("source-only diagnostic\n")
            (raw / "destination.trace.log").write_text("usb_host_open_success\n")
            (raw / "destination.serial.log").write_text("New USB device found\n")
            result = run.write_logs(output, {
                "result": "PASS", "vm_migration_passed": True, "usb_reenumeration_passed": True,
                "source_status": {"status": "postmigrate"},
                "destination_status": {"status": "running"},
            }, journal, raw)
            self.assertIn("虚拟机迁移：成功", result)
            self.assertIn("usb_host_release_interface", (output / "source.log").read_text())
            destination = (output / "destination.log").read_text()
            self.assertIn("usb_host_open_success", destination)
            self.assertIn("New USB device found", destination)
            self.assertNotIn("source-only diagnostic", destination)
            migration = (output / "migration.log").read_text()
            self.assertLess(migration.index("physical device released"), migration.index('"cont"'))
            self.assertEqual({path.name for path in output.iterdir()},
                             {"result.log", "source.log", "destination.log", "migration.log"})

    def test_usb_failure_does_not_hide_completed_vm_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run.write_logs(Path(directory), {
                "result": "FAIL", "vm_migration_passed": True,
                "usb_reenumeration_passed": False, "error": "USB did not reconnect",
            })
            self.assertIn("实验结果：FAIL", result)
            self.assertIn("虚拟机迁移：成功", result)
            self.assertIn("物理 USB 重新枚举：未完成", result)
            self.assertIn("USB did not reconnect", result)


class HandoffTests(unittest.TestCase):
    def exercise(self, dest_running=False, early_claim=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name)
        (output / "migration.sock").touch()
        trace = "\n".join(["usb_host_open_success", "usb_port_detach", "usb_port_attach", "usb_host_set_config"])
        (output / "destination.trace.log").write_text(trace if early_claim else "")
        (output / "source.trace.log").write_text(trace)
        journal = run.Journal(output / "migration.log")
        self.addCleanup(journal.file.close)
        args = argparse.Namespace(timeout=1)
        experiment = run.Experiment(args, output, journal, "unused", output,
                                    {"vid": "046a", "pid": "b090"}, output)
        order = []

        class Monitor:
            def command(self, command, arguments=None):
                order.append(command)
                return {"status": "completed"}

        def launch(role):
            monitor = Monitor()
            experiment.monitors[role] = monitor
            return monitor

        def snapshot(role, stage):
            if role == "source":
                return {"status": {"status": "postmigrate", "running": False}}
            return {"status": {"status": "paused" if stage == "before-handoff" else "running",
                                "running": dest_running if stage == "before-handoff" else True}}

        def stop_source():
            order.append("source-exit")
            (output / "destination.trace.log").write_text(trace)

        def observations(role):
            return [{"boot": "same-boot", "seq": 1 if role == "source" else 9,
                     "devnum": 2 if role == "source" else 3}]

        experiment.launch = launch
        experiment.snapshot = snapshot
        experiment.stop_source = stop_source
        # Exercise the real evidence predicate with advancing guest observations.
        def wait(description, predicate, timeout=None):
            if description == "destination guest continues running":
                experiment.observations = lambda role: [{"boot": "same-boot", "seq": 12, "devnum": 3}]
            result = predicate()
            if not result:
                raise TimeoutError(description)
            return result
        experiment.observations, experiment.wait = observations, wait
        return experiment, order

    def test_source_released_before_destination_cont(self):
        experiment, order = self.exercise()
        result = experiment.run()
        self.assertLess(order.index("source-exit"), order.index("cont"))
        self.assertTrue(result["usb_reenumeration_passed"])

    def test_running_destination_before_handoff_fails(self):
        experiment, order = self.exercise(dest_running=True)
        with self.assertRaisesRegex(RuntimeError, "unexpectedly running"):
            experiment.run()
        self.assertNotIn("cont", order)

    def test_early_physical_claim_fails(self):
        experiment, order = self.exercise(early_claim=True)
        with self.assertRaisesRegex(RuntimeError, "before handoff"):
            experiment.run()
        self.assertNotIn("migrate", order)

    def test_missing_source_physical_evidence_fails(self):
        experiment, order = self.exercise()
        (experiment.output / "source.trace.log").write_text("")
        with self.assertRaisesRegex(RuntimeError, "Missing source physical USB evidence"):
            experiment.run()
        self.assertNotIn("migrate", order)

    def test_missing_destination_reconnect_evidence_fails(self):
        experiment, _ = self.exercise()
        stop_source = experiment.stop_source

        def release_without_reconnect_evidence():
            stop_source()
            (experiment.output / "destination.trace.log").write_text("usb_host_open_success")

        experiment.stop_source = release_without_reconnect_evidence
        with self.assertRaisesRegex(RuntimeError, "Missing USB reconnect trace evidence"):
            experiment.run()
        self.assertFalse(experiment.findings["usb_reenumeration_passed"])

    def test_unchanged_usb_address_cannot_pass_reenumeration(self):
        experiment, _ = self.exercise()
        experiment.observations = lambda role: [
            {"boot": "same-boot", "seq": 1 if role == "source" else 9, "devnum": 2}
        ]
        with self.assertRaisesRegex(TimeoutError, "destination guest configured USB device"):
            experiment.run()


class FailureCleanupTests(unittest.TestCase):
    def test_failed_run_keeps_only_four_logs_and_removes_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin/qemu-system-x86_64"
            binary.parent.mkdir()
            binary.touch()
            revision = root / "share/qemu/experiment-revision"
            revision.parent.mkdir(parents=True)
            revision.write_text(run.REVISION)
            (root / "kernel").touch()
            (root / "initrd").touch()
            output = root / "logs"
            temporary = []

            def fail(experiment):
                temporary.append(experiment.runtime)
                (experiment.runtime / "source.trace.log").write_text("usb_host_open_success\n")
                raise RuntimeError("injected migration failure")

            env = {"EXPERIMENT_QEMU": str(binary), "EXPERIMENT_GUEST": str(root),
                   "XDG_RUNTIME_DIR": str(root), "EXPERIMENT_DIRECTORY": str(root)}
            with patch.dict(os.environ, env), patch.object(sys, "argv", [
                    "run.py", "--usb", "unit-test", "--accel", "tcg", "--output", str(output)]), \
                    patch.object(run, "devices", return_value=[]), \
                    patch.object(run, "select_device", return_value={"port": "unit-test"}), \
                    patch.object(run, "check_device"), \
                    patch.object(run.Experiment, "run", fail), redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(), 1)
            self.assertEqual({path.name for path in output.iterdir()},
                             {"result.log", "source.log", "destination.log", "migration.log"})
            self.assertIn("usb_host_open_success", (output / "source.log").read_text())
            self.assertIn("injected migration failure", (output / "result.log").read_text())
            self.assertTrue(temporary)
            self.assertTrue(all(not path.exists() for path in temporary))

    def test_hardware_test_requires_a_real_device_selector(self):
        hardware = Path(__file__).resolve().parents[1] / "hardware"
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(hardware)],
                cwd=directory, capture_output=True, text=True,
                env=os.environ | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
            self.assertEqual(completed.returncode, 4, completed.stdout + completed.stderr)
            self.assertIn("A real USB device is required", completed.stderr)
            self.assertFalse((Path(directory) / "results").exists())

    def test_hardware_test_has_no_emulated_device_option(self):
        hardware = Path(__file__).resolve().parents[1] / "hardware"
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(hardware), "--smoke"],
                cwd=directory, capture_output=True, text=True,
                env=os.environ | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
            self.assertEqual(completed.returncode, 4, completed.stdout + completed.stderr)
            self.assertIn("unrecognized arguments: --smoke", completed.stderr)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin/qemu-system-x86_64"
            binary.parent.mkdir()
            binary.touch()
            revision = root / "share/qemu/experiment-revision"
            revision.parent.mkdir(parents=True)
            revision.write_text(run.REVISION)
            (root / "kernel").touch()
            (root / "initrd").touch()
            output = root / "existing"
            output.mkdir()
            result = output / "result.log"
            result.write_text("保留的实验日志")
            env = {"EXPERIMENT_QEMU": str(binary), "EXPERIMENT_GUEST": str(root),
                   "EXPERIMENT_DIRECTORY": str(root)}
            with patch.dict(os.environ, env), patch.object(sys, "argv", [
                    "run.py", "--usb", "1-2", "--accel", "tcg", "--output", str(output)]), \
                    patch.object(run, "devices", return_value=[]), \
                    patch.object(run, "select_device", return_value={"port": "1-2"}), \
                    patch.object(run, "check_device"):
                self.assertEqual(run.main(), 1)
            self.assertEqual(result.read_text(), "保留的实验日志")

    def test_child_is_terminated_if_qmp_is_broken(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = run.Journal(root / "migration.log")
            experiment = run.Experiment(argparse.Namespace(), root, journal,
                                        "unused", root, {}, root)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            experiment.processes["source"] = child
            try:
                experiment.cleanup()
                self.assertIsNotNone(child.poll())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                journal.file.close()


if __name__ == "__main__":
    unittest.main()
