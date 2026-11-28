"""Locate the writable experiment checkout and keep all results inside it."""
import datetime as dt
import os
from pathlib import Path
import tempfile


def experiment_directory():
    configured = os.environ.get("EXPERIMENT_DIRECTORY")
    if configured:
        return Path(configured).resolve()
    local = Path(__file__).resolve().parents[1]
    if not local.is_relative_to("/nix/store") and (local / "run.py").is_file():
        return local
    for parent in (Path.cwd(), *Path.cwd().parents):
        candidate = parent / "experiments/qemu-physical-device"
        if (candidate / "run.py").is_file():
            return candidate.resolve()
    raise RuntimeError("Cannot locate the experiment checkout; run from the repository or use its run.sh")


def create_output_directory(requested=None):
    experiment = experiment_directory()
    os.environ["EXPERIMENT_DIRECTORY"] = str(experiment)
    if requested is not None:
        path = Path(requested)
        output = (path if path.is_absolute() else experiment / path).resolve()
        if not output.is_relative_to(experiment):
            raise RuntimeError("--output must stay inside the experiment directory")
        output.mkdir(parents=True, mode=0o700)
        return output
    parent = experiment / "results"
    parent.mkdir(parents=True, exist_ok=True)
    prefix = dt.datetime.now().strftime("%Y%m%d-%H%M%S-")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
