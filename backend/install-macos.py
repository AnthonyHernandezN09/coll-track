#!/usr/bin/env python3
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

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
for filename in ("server.py", "brandfetch.py", "portal_monitor.py", "portal-reader.js", "index.html", "dashboard.js", "dashboard.css"):
    shutil.copyfile(Path(__file__).with_name(filename), support / filename)
for filename in ("checklist-reader.js", "slate-login.js"):
    shutil.copyfile(Path(__file__).parent.parent / filename, support / filename)
subprocess.run(["swiftc", str(Path(__file__).with_name("vault-crypto.swift")),
                "-o", str(support / "vault-crypto")], check=True)
agent.write_bytes(plistlib.dumps({
    "Label": label,
    "ProgramArguments": [str(runtime), str(server), "--monitor"],
    "EnvironmentVariables": {"PLAYWRIGHT_BROWSERS_PATH": str(support / "browsers")},
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 5,
}))
subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True)
(support / "pairing-code").unlink(missing_ok=True)
print("Coll-Track is available at http://127.0.0.1:46387/")
