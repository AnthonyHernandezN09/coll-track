#!/usr/bin/env python3
import argparse
import os
import plistlib
import shutil
from shlex import quote
import subprocess
import sys
import time
from pathlib import Path

def choose_installation():
    parser = argparse.ArgumentParser(description="Install the Coll-Track service and optional clients.")
    parser.add_argument("--preset", choices=("default", "lite", "custom"),
                        help="Choose an installation without opening the menu")
    dashboard = parser.add_mutually_exclusive_group()
    dashboard.add_argument("--dashboard", dest="dashboard", action="store_true")
    dashboard.add_argument("--no-dashboard", dest="dashboard", action="store_false")
    parser.set_defaults(dashboard=None)
    parser.add_argument("--cli", action="store_true", help="Install the command-line client")
    parser.add_argument("--tui", action="store_true", help="Install the terminal menu")
    parser.add_argument("--extension", action="store_true", help="Print the Chrome extension folder")
    args = parser.parse_args()
    component_flags = args.dashboard is not None or args.cli or args.tui or args.extension
    preset = args.preset
    if component_flags and preset in ("default", "lite"):
        parser.error("Choose --preset custom to select individual clients.")

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not preset and not component_flags and interactive:
        print("1. Default (everything): dashboard, CLI, terminal menu, and Chrome extension path")
        print("2. Lite: CLI and terminal menu")
        print("3. Custom: choose each client")
        while not preset:
            choice = input("Installation [1]: ").strip().lower() or "1"
            preset = {"1": "default", "2": "lite", "3": "custom"}.get(choice)
            if not preset:
                print("Enter 1, 2, or 3.")

    if preset == "custom" and not component_flags and interactive:
        for name, description in (
            ("dashboard", "Enable the dashboard"),
            ("cli", "Install the CLI"),
            ("tui", "Install the terminal menu"),
            ("extension", "Show the Chrome extension path"),
        ):
            while True:
                answer = input(f"{description}? [y/N]: ").strip().lower()
                if answer in ("", "n", "no", "y", "yes"):
                    setattr(args, name, answer in ("y", "yes"))
                    break
                print("Enter y or n.")
    elif not preset:
        if component_flags:
            # Preserve the dashboard default for existing commands such as --cli.
            args.dashboard = args.dashboard is not False
        else:
            preset = "default"

    if preset in ("default", "lite"):
        args.dashboard = args.extension = preset == "default"
        args.cli = args.tui = True
    return args


try:
    args = choose_installation()
except (EOFError, KeyboardInterrupt):
    sys.exit("Installation cancelled.")

if sys.platform != "darwin":
    sys.exit("The Coll-Track service installer requires macOS.")
if sys.version_info < (3, 9):
    sys.exit("Coll-Track requires Python 3.9 or later.")
if not shutil.which("swiftc") or subprocess.run(
    ["xcode-select", "-p"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
).returncode:
    sys.exit("Install the Xcode command-line tools with xcode-select --install, then run this installer again.")

cargo = shutil.which("cargo")
if args.cli or args.tui:
    if not cargo:
        candidate = Path.home() / ".cargo/bin/cargo"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            cargo = str(candidate)
    if not cargo:
        sys.exit("The CLI and terminal menu need Rust and Cargo. Install Rust 1.89 or later, or choose Custom without terminal tools.")

label = "local.coll-track.backend"
support = Path.home() / "Library/Application Support/Coll-Track"
agents = Path.home() / "Library/LaunchAgents"
agent = agents / f"{label}.plist"
domain = f"gui/{os.getuid()}"

os.umask(0o077)
support.mkdir(parents=True, exist_ok=True, mode=0o700)
agents.mkdir(parents=True, exist_ok=True)
runtime = support / "runtime" / "bin" / "python"
if not runtime.is_file():
    subprocess.run([sys.executable, "-m", "venv", str(support / "runtime")], check=True)
browser_environment = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(support / "browsers")}
subprocess.run([str(runtime), "-m", "pip", "install", "playwright==1.60.0"], check=True)
subprocess.run([str(runtime), "-m", "playwright", "install", "chromium", "--only-shell"],
               env=browser_environment, check=True)
server = support / "server.py"
if args.cli or args.tui:
    manifest = Path(__file__).resolve().parents[1] / "cli/Cargo.toml"
    target = Path(os.environ.get("CARGO_TARGET_DIR") or support / "rust-target").resolve()
    subprocess.run([cargo, "build", "--release", "--locked", "--manifest-path", str(manifest),
                    "--target-dir", str(target)], check=True)
    terminal = support / "coll-track"
    replacement = terminal.with_name(terminal.name + ".new")
    shutil.copy2(target / "release/coll-track", replacement)
    replacement.replace(terminal)
    launchers = Path.home() / ".local/bin"
    launchers.mkdir(parents=True, exist_ok=True, mode=0o755)
    commands = []
    if args.cli:
        commands.extend([("coll-track", ""), ("cltrk", "")])
    if args.tui:
        commands.append(("cltrk-tui", " menu"))
    for name, command in commands:
        launcher = launchers / name
        replacement = launcher.with_name(name + ".new")
        replacement.write_text(f'#!/bin/sh\nexec {quote(str(terminal))}{command} "$@"\n')
        replacement.chmod(0o755)
        replacement.replace(launcher)

for filename in ("server.py", "brandfetch.py", "portal_monitor.py", "portal-reader.js", "index.html", "dashboard.js", "dashboard.css"):
    shutil.copyfile(Path(__file__).with_name(filename), support / filename)
for filename in ("checklist-reader.js", "slate-login.js"):
    shutil.copyfile(Path(__file__).parent.parent / filename, support / filename)
subprocess.run(["swiftc", str(Path(__file__).with_name("vault-crypto.swift")),
                "-o", str(support / "vault-crypto")], check=True)
agent.write_bytes(plistlib.dumps({
    "Label": label,
    "ProgramArguments": [str(runtime), str(server), "--monitor"] + (["--no-dashboard"] if not args.dashboard else []),
    "EnvironmentVariables": {"PLAYWRIGHT_BROWSERS_PATH": str(support / "browsers")},
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 5,
}))
subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
for attempt in range(5):
    result = subprocess.run(["launchctl", "bootstrap", domain, str(agent)], capture_output=True)
    if result.returncode == 0:
        break
    if attempt == 4:
        sys.stderr.buffer.write(result.stderr)
        result.check_returncode()
    time.sleep(0.5)
(support / "pairing-code").unlink(missing_ok=True)
print("Coll-Track service installed on this Mac.")
if args.dashboard:
    print("Dashboard: http://127.0.0.1:46387/")
if args.cli or args.tui:
    print(f"Terminal launchers: {launchers}")
    if str(launchers) not in os.get_exec_path():
        command = "cltrk" if args.cli else "cltrk-tui"
        print(f"Run ~/.local/bin/{command}, or add ~/.local/bin to your PATH.")
if args.extension:
    print(f"Chrome extension folder: {Path(__file__).resolve().parents[1]}")
    print("In Chrome, open chrome://extensions, enable Developer mode, and choose Load unpacked.")
