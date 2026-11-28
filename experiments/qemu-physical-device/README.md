# Same-host QEMU migration test with a physical USB device

This experiment uses a **pytest hardware integration test running directly on the host**.
It starts source and destination QEMU processes on the same physical NixOS host,
passes through a real USB device selected on the command line using `usb-host`,
and automates migration, device handoff, log collection, and result checks.

The test case is [`hardware/test_physical_usb.py`](hardware/test_physical_usb.py).
Runtime configuration is defined in [`hardware/conftest.py`](hardware/conftest.py),
and migration orchestration is implemented in [`run.py`](run.py).
You must explicitly select a real device. Missing devices, insufficient permissions,
or unmet acceptance criteria cause the test to fail.
The production entry points create `usb-host` devices only; they provide no `usb-kbd`
or `--smoke` mode.

Nix pins the dependencies and builds the environment. Pytest accesses host hardware
when invoked. The hardware experiment runs through `nix run` so that hardware access
is outside the Nix build sandbox and a cached successful build cannot replace a fresh
experiment. Source and destination run directly on the physical host, without an
additional outer test VM.

## Environment and versions

| Component | Pinned value |
| --- | --- |
| Host | x86_64 Linux / NixOS with the Nix `nix-command flakes` features enabled |
| QEMU repository | <https://github.com/ziyifu225/qemu> |
| QEMU source branch | `master-thesis` |
| QEMU source revision | [`005c61c0ef6f6ae615283440b4bb7be51b2c6ddd`](https://github.com/ziyifu225/qemu/tree/005c61c0ef6f6ae615283440b4bb7be51b2c6ddd) |
| QEMU VERSION | `11.1.3` in this fork |
| Local source checkout | Git submodule at `source/qemu-usb-11.1.3` |
| nixpkgs | `da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36`; content hashes are recorded in `flake.lock` |
| Guest kernel | Linux `6.18.57` from the pinned nixpkgs |
| Guest userspace | BusyBox initramfs running in memory, with no guest disk or network |
| Machine | `pc-q35-11.1`, 1 vCPU, 256 MiB RAM |
| Acceleration | KVM by default; optional `--accel tcg` still uses the same real USB device |
| USB topology | Emulated `qemu-xhci` controller plus `usb-host` passthrough of the physical device |
| Migration transport | Private Unix socket on the same host |
| Test framework | Python and pytest from the pinned nixpkgs |

The build is defined in [`default.nix`](default.nix). It explicitly enables libusb,
KVM, TCG, and trace logging, and disables Rust, documentation, and downloads during
the build. The keycodemapdb, SoftFloat, and TestFloat revisions are pinned in
`flake.lock` to match the `.wrap` files in the selected QEMU commit.

Nix builds a pinned source archive of the same commit as the submodule. The submodule
is available for local source inspection and development. `.gitmodules` specifies
the repository URL and `branch = master-thesis`; the parent repository's gitlink pins
the exact commit above. The experiment uses that commit rather than following the
latest branch tip. Editing the submodule working tree does not automatically change
the Nix build. To study another version, update the gitlink, flake input, version
checks in the experiment, and lock file together.

## Run the physical-device test with one command

First, list the available devices:

```sh
nix run .#qemu-physical-device -- --list
```

The output includes the sysfs physical port, `lsusb` bus/address, VID:PID, product
name, and serial number. Select a device dedicated to the experiment, then run:

**For a device shown by `lsusb` as `Bus 001 Device 005`, pass `--usb-address 001:005`.**
Enter the target device number on each invocation; there is no need to edit the test
file. The test operates only on the device you select. It reports an error if the
device is missing, the selector is ambiguous, or permissions are insufficient, and
does not select another device automatically.

```sh
# Enter the lsusb Bus:Device; the script first resolves it to a physical port
nix run .#qemu-physical-device-test -- --usb-address 001:005

# Alternatively, select an exact physical port
nix run .#qemu-physical-device-test -- --usb 1-2.3

# Select a unique VID:PID; multiple matches cause an error
nix run .#qemu-physical-device-test -- --usb-id 046a:b090

# Script entry point
./experiments/qemu-physical-device/run.sh --usb 1-2.3

# Use software CPU emulation when KVM is unavailable; USB remains physical
nix run .#qemu-physical-device-test -- --usb 1-2.3 --accel tcg --timeout 300

# Specify a result directory that does not already exist
nix run .#qemu-physical-device-test -- --usb 1-2.3 --output results/first-real-usb
```

The device numbers, ports, and VID:PID values above are examples. Replace them with
the actual device identifiers. The first invocation builds QEMU and the guest;
subsequent invocations reuse the build artifacts. Every test invocation takes
ownership of the physical device again, performs migration, and produces new logs.
The pytest exit code is 0 for success, 1 for a test failure, and 4 for configuration
errors such as a missing device selector. The output prints the result directory.

By default, logs are stored under `results/<run-id>/` inside this experiment's
directory: `experiments/qemu-physical-device/results/<run-id>/` in the repository.
Relative `--output` paths are also resolved against the experiment directory and
cannot point outside it. Nix entry points locate this directory when run from the
repository root or a subdirectory. You can invoke `run.sh` by its absolute path
from another working directory.

QEMU temporarily detaches the device from its host driver and resets it when
releasing it. If you test a USB keyboard, use another keyboard to control the host.
The script rejects USB hubs, devices without read/write access, and USB storage
that is mounted in the current mount namespace or used as swap, including partitions
and stacked device-mapper devices. The guest does not mount, format, or write test
data to physical storage.

## Run the test file directly

```sh
nix develop --command pytest -v \
  experiments/qemu-physical-device/hardware/test_physical_usb.py --usb 1-2.3
```

The adjacent `conftest.py` defines the device arguments, log directory, and result
output. A single pytest test case runs the complete experiment: it invokes the
pinned migration runner and checks the hardware evidence, migration outcome, and
cleanup. This file can serve as a template for organizing future hardware tests.

To invoke the migration runner directly:

```sh
nix run .#qemu-physical-device -- --usb 1-2.3
```

This entry point also requires a real USB device and writes the result and both
peers' logs.

## NixOS permissions: one-time setup

Your user account needs read/write access to `/dev/kvm` and the selected
`/dev/bus/usb/BBB/DDD` node. Add a narrowly scoped udev rule for the experiment device
to your NixOS configuration:

```nix
{
  users.groups.usb-experiment = { };
  users.users.YOUR_USER.extraGroups = [ "kvm" "usb-experiment" ];
  services.udev.extraRules = ''
    SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="046a", ATTR{idProduct}=="b090", MODE="0660", GROUP="usb-experiment"
  '';
}
```

Replace the username and device IDs. If several devices share these IDs, add
`ATTR{serial}=="..."` or a physical-port restriction. Apply the configuration,
log out and back in, and unplug and reconnect the target device before running
the experiment. The udev rule continues to grant access if a USB reset changes
the device address. The test program itself does not modify host configuration
or device permissions.

### Write the configuration from the command line

The following commands apply to the current host: its configuration is in
`/etc/nixos`, its flake configuration is named `nixos`, and the imports in
`configuration.nix` already contain `./hardware-configuration.nix`.
Run these commands in a terminal as your normal user. First replace the VID/PID
below with those of the target device. You can find them with `--list`, for example
`046a:b090`. VID:PID is different from a bus/address selector such as `001:005`.

```sh
# Replace these with the actual device VID/PID
usb_vid=046a
usb_pid=b090
usb_user=$(id -un)

sudo tee /etc/nixos/usb-experiment.nix >/dev/null <<EOF
{ lib, ... }:
{
  users.groups.usb-experiment = { };
  users.users."$usb_user".extraGroups = lib.mkAfter [ "kvm" "usb-experiment" ];
  services.udev.extraRules = ''
    SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="$usb_vid", ATTR{idProduct}=="$usb_pid", MODE="0660", GROUP="usb-experiment"
  '';
}
EOF

# Add the module to the existing imports unless it is already imported
if ! grep -Eq '^[[:space:]]*\./usb-experiment\.nix[[:space:]]*$' /etc/nixos/configuration.nix; then
  sudo sed -i '/^[[:space:]]*\.\/hardware-configuration\.nix[[:space:]]*$/a\      ./usb-experiment.nix' /etc/nixos/configuration.nix
fi

# Apply the system configuration
sudo nixos-rebuild switch --flake /etc/nixos#nixos
```

Afterward, log out of your desktop session and back in, reconnect the experiment
device, and check that `id -nG` includes `kvm` and `usb-experiment`. Look up the device
number again before starting the test, because reconnecting may change its Device
number. `lib.mkAfter` appends the required groups to the list declared by other
NixOS modules. Add a serial-number or physical-port match to the rule if access
should be restricted to one particular device of the same model.

### USB access is still denied after applying the configuration

Compare the groups of the current session with the configured account membership:

```sh
id
id "$(id -un)"
```

If the second command includes `usb-experiment` but the first does not, the current
session still has its old group membership. Log out of the desktop session and back
in to refresh it. Opening another terminal may still inherit the desktop session's
old group list. You can also enable the group for a single experiment invocation:

```sh
# Replace VID:PID with the target device; this example uses 2717:5013
sg usb-experiment -c 'nix run .#qemu-physical-device-test -- --usb-id 2717:5013'
```

Alternatively, run `newgrp usb-experiment` to start a new shell, then run the
original experiment command. There is no need to rewrite the udev rule in this case.

## Experiment procedure and expected behavior

1. Locate the physical USB device and record its hostbus, hostport, VID:PID, serial
   number, and device node. Check permissions and device usage, then acquire a
   per-user lock for the port.
2. Start source, pass through the device, and wait for the guest to configure it.
   Require source trace evidence of a successful physical-device open and a
   configuration request.
3. Start destination with the same virtual topology and `-S -incoming defer`.
   Keep it paused so it does not claim the hardware prematurely.
4. Initiate migration through QMP and record commands, responses, received
   asynchronous events, and both peers' logs.
5. Check that source migration has completed and source is in `postmigrate`, while
   destination has received the migration and remains paused. Verify that
   destination has not already opened the physical USB device successfully.
6. Quit source and wait for the process to exit, releasing its libusb handle.
   Then resume destination.
7. Wait for destination to reopen the physical device and for the guest to
   re-enumerate and configure it. Verify that the guest continues running.
8. Quit destination and save the results. On failure, timeout, Ctrl-C, or SIGTERM,
   clean up the processes started by this runner and preserve failure logs.
   Automatic cleanup cannot be guaranteed after SIGKILL or a host power loss.

In the pinned QEMU version, `usb_host_post_load_bh()` emulates a USB surprise removal,
then scans for the device and attaches it again. Device firmware state, internal
buffers, and in-flight physical I/O are not migrated. The expected observation is
**completed VM state migration, physical-device ownership acquired by destination,
and USB re-enumeration in the guest**. The test checks recovery through reconnection;
it does not check uninterrupted application I/O or continuity of open device sessions.

Source's exit notifier releases interfaces, resets the device, reattaches host
kernel drivers, and closes the libusb handle. Destination is deliberately paused
before handoff so that it acquires the device only after source has released it.

### Real log example: disconnect, ownership handoff, and re-enumeration

The following excerpts come from `results/20261128-003235-3r9emt9m/`, using a physical
Mi Wireless Mouse (`2717:5013`). Only lines relevant to the device handoff are shown.

First, [source.log](results/20261128-003235-3r9emt9m/source.log) records source releasing
the device interface and executing the host kernel driver reattachment path:

```text
usb_host_release_interface dev 3:83, if 0
usb_host_attach_kernel dev 3:83, if 0
```

[migration.log](results/20261128-003235-3r9emt9m/migration.log) then records
`source exited; physical device released`, followed by the runner sending `cont`
to destination.

The QEMU trace in [destination.log](results/20261128-003235-3r9emt9m/destination.log)
shows the virtual USB port being detached, followed by opening the same physical
device, attaching it to the virtual port, configuring it, and claiming its interface:

```text
usb_port_detach bus 0, port 1
usb_host_open_started dev 3:83
usb_port_attach bus 0, port 1, devspeed full, portspeed full+high+super
usb_host_open_success dev 3:83
usb_host_set_config dev 3:83, config 1
usb_host_claim_interface dev 3:83, config 1, if 0
```

The guest kernel messages in the same `destination.log` provide the clearest evidence
of a disconnect followed by reattachment:

```text
[    3.364340] usb 1-1: USB disconnect, device number 2
[    3.751906] usb 1-1: new full-speed USB device number 3 using xhci_hcd
[    3.886010] usb 1-1: New USB device found, idVendor=2717, idProduct=5013, bcdDevice= 1.00
```

The guest's previous USB device **2** disconnected, and a device with the same
VID:PID was subsequently enumerated as **3**. QEMU triggers this disconnect and
reattachment at the virtual USB port; the physical device on the host is still
`3:83`. The observed sequence is: **source releases the physical device → destination
acquires it again → the guest observes disconnection and re-enumeration**.

## Success criteria

The pytest test must verify all of the following:

- Each peer's launch command contains exactly one `usb-host`, with selectors matching
  the physical hostbus/hostport and VID:PID.
- Both source and destination traces show a successful physical USB open and
  configuration.
- Source reports `query-migrate=completed` and status `postmigrate`; destination
  ultimately reports `running`.
- `migration.log` shows that source fully exits before destination resumes.
- The guest boot UUID remains unchanged, its counter advances, and the target
  VID:PID matches. USB configuration is greater than 0, and the guest assigns a
  new USB devnum.
- Destination trace contains port detachment, attachment, a successful physical
  device open, and configuration events.
- Destination exits and is reaped. Both migration and USB re-enumeration are
  reported as successful in `result.log`.

The generic guest includes xHCI, USB HID, and USB storage/UAS drivers. Some devices
may need additional drivers, firmware, or recovery steps. A passing result confirms
the migration and re-enumeration criteria above; it does not validate keyboard
input, storage contents, or other application-level device functionality.

## Output

Each run retains only the following four files. Read `result.log` first, then
compare the peers' logs and the handoff sequence.

```text
experiments/qemu-physical-device/results/<run-id>/
  result.log                 Migration/reconnection outcome, peer states, and failure reasons
  source.log                 Source launch command, QMP, USB trace, errors, and guest log
  destination.log            Destination launch command, QMP, USB trace, errors, and guest log
  migration.log              Peer operations, QMP states, and device handoff order
```

If the device is missing or preflight checks fail, the reason is written to
`result.log` and `migration.log`. The peer logs will contain no actual startup or
device operations. Existing result directories are never overwritten.

### What to look for in the logs

| Log / event | Meaning |
| --- | --- |
| Source `usb_host_open_success`, `usb_host_set_config` | Source has opened and configured the selected physical USB device |
| Source QMP `query-migrate` returns `completed`, `query-status` is `postmigrate` | VM state has migrated out of source |
| `migration.log`: `source exited; physical device released`, followed by destination `cont` | The runner resumes destination only after source exits and releases the device |
| Destination `vmstate_load_state` / `usb-host` | Destination is loading the USB device's VMState |
| Destination `usb_port_detach` | USB removal is emulated after loading the state |
| Destination `usb_host_open_success`, `usb_port_attach`, `usb_host_set_config` | Destination has reopened, attached, and configured the device |
| Destination guest `New USB device found` and a new `devnum` | The guest has re-enumerated the physical USB device |
| Destination QMP `query-status` is `running`, and the guest counter advances | The guest continues running after migration |

`source.log` and `destination.log` are divided into QMP, trace, error, and guest-log
sections. The launch command is also recorded in the QMP section. `migration.log`
provides the operation order across both peers; timestamps allow events to be
correlated. If VM migration completes but USB recovery fails, `result.log` reports
the two outcomes separately.

## Development checks and validation status

```sh
# Check control logic only; no physical hardware is claimed
nix flake check -L

# Build the pinned environment only
nix build .#qemu-physical-device-test
```

`nix build` builds the test program without executing the hardware experiment.
The unit tests in `nix flake check` use controlled inputs to check device selection,
QMP handling, handoff ordering, rejection of insufficient evidence, and failure
cleanup. Their results do not constitute a physical-device experiment. To run the
hardware experiment, explicitly invoke
`nix run .#qemu-physical-device-test -- --usb ...`.

The physical-device run `20261128-003235-3r9emt9m` on 2026-11-28 used a Mi Wireless
Mouse (`2717:5013`) and produced PASS in `result.log`. The logs record source
releasing the interface, destination acquiring the device, and the guest
re-enumerating it from device number 2 to 3. The relevant excerpts are shown above.
The 26 control-logic unit tests have passed. Both the Nix entry point and direct
pytest invocation have also been checked to fail and record the reason when the
device is missing. Omitting the device selector produces a configuration error.

## Source references

- [host-libusb.c](https://github.com/ziyifu225/qemu/blob/005c61c0ef6f6ae615283440b4bb7be51b2c6ddd/hw/usb/host-libusb.c):
  `usb_host_post_load()`, `usb_host_post_load_bh()`, `usb_host_auto_check()`, `usb_host_exit_notifier()`.
- [USB trace-events](https://github.com/ziyifu225/qemu/blob/005c61c0ef6f6ae615283440b4bb7be51b2c6ddd/hw/usb/trace-events): log event definitions.
- [VERSION](https://github.com/ziyifu225/qemu/blob/005c61c0ef6f6ae615283440b4bb7be51b2c6ddd/VERSION): experiment version.
