# vfio-user-live-migration-test

Device migration experiments on NixOS, with source revisions and build environments pinned through Nix.

## Experiments

- [Same-host QEMU migration with a physical USB device](experiments/qemu-physical-device/README.md)

See each experiment's README for its environment, usage, permissions, and results.

## Repository structure

- `experiments/`: Test files, documentation, and results for each experiment.
- `source/`: Git submodules pinned to specific project revisions.
- `flake.nix` / `flake.lock`: Nix build entry points and dependency locks.
