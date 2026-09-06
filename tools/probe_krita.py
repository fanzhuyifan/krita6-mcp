"""Run fixed native API checks in a disposable Krita profile (Linux)."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


def isolated_environment(base: Path) -> dict:
    """Isolate resources and QtSingleApplication sockets, not just settings."""
    for name in ("config", "data", "cache", "runtime"):
        (base / name).mkdir(parents=True, exist_ok=True)
    (base / "runtime").chmod(0o700)
    env = dict(os.environ)
    env.update(
        XDG_CONFIG_HOME=str(base / "config"),
        XDG_DATA_HOME=str(base / "data"),
        XDG_CACHE_HOME=str(base / "cache"),
        XDG_RUNTIME_DIR=str(base / "runtime"),
        TMPDIR=str(base / "runtime"),
        QT_QPA_PLATFORM="xcb",
        QT_QPA_PLATFORMTHEME="",
        QT_STYLE_OVERRIDE="Fusion",
        XDG_CURRENT_DESKTOP="",
        LIBGL_ALWAYS_SOFTWARE="1",
    )
    return env


@contextmanager
def launch_krita(base: Path, executable: str, env: dict):
    for program in ("xvfb-run", "dbus-run-session", executable):
        if shutil.which(program) is None:
            raise RuntimeError(f"Required host-test executable is missing: {program}")
    with (base / "krita.log").open("w") as log:
        process = subprocess.Popen(
            ["xvfb-run", "-a", "dbus-run-session", "--", executable, "--nosplash"],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            # Only the process group created by this test is signalled. In
            # particular, never stop a user's existing Krita process.
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--krita", default="krita")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = (args.output or Path(tempfile.mkdtemp(prefix="krita6-probe-"))).resolve()
    if (base / "report.json").exists():
        parser.error("Output already contains a report; choose a new directory.")
    env = isolated_environment(base)
    plugins = base / "data" / "krita" / "pykrita"
    (plugins / "native_probe").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        root / "tests" / "host" / "native_probe.py", plugins / "native_probe" / "__init__.py"
    )
    (plugins / "native_probe.desktop").write_text(
        "[Desktop Entry]\nType=Service\nServiceTypes=Krita/PythonPlugin\n"
        "X-KDE-Library=native_probe\nX-Python-2-Compatible=false\nName=Native API Probe\n"
    )
    (base / "config" / "kritarc").write_text("[python]\nenable_native_probe=true\n")
    env["KRITA6_PROBE_OUTPUT"] = str(base)
    with launch_krita(base, args.krita, env) as process:
        try:
            process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            print(f"Krita probe timed out. Log: {base / 'krita.log'}", file=sys.stderr)
            return 1
    report_path = base / "report.json"
    if not report_path.exists():
        print(
            f"Probe did not write a report (exit {process.returncode}). Log: {base / 'krita.log'}",
            file=sys.stderr,
        )
        return 1
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2))
    print(f"Artifacts: {base}")
    return 0 if report.get("passed") and process.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
