"""The image ships everything the app imports, and the live code and archive/ stay independent."""

import ast
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive"


def imported_modules(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def stems(directory):
    return {path.stem for path in directory.glob("*.py")}


def dockerfile():
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


class PackagingTests(unittest.TestCase):
    def test_the_image_copies_every_local_module_the_app_imports(self):
        sources = {name for line in dockerfile().splitlines() if line.startswith("COPY ")
                   for name in line.split()[1:-1]}
        copied = {name for name in sources if name.endswith(".py")}
        self.assertIn("web_app.py", copied)
        for module in copied:
            for name in imported_modules(ROOT / module) & stems(ROOT):
                self.assertIn(f"{name}.py", copied, f"{module} imports {name}, which the image does not copy")
        allowed = set((ROOT / ".dockerignore").read_text(encoding="utf-8").split())
        for source in sources:
            self.assertIn(f"!{source}", allowed, f"{source} is copied but excluded from the build context")
        self.assertFalse(any("archive" in entry for entry in allowed))
        self.assertNotIn("archive", dockerfile())

    def test_the_health_check_follows_the_configured_port(self):
        healthcheck = re.search(r"^HEALTHCHECK [^\n]*\\\n\s+CMD (\[[^\n]*\])$", dockerfile(), re.M)
        command = json.loads(healthcheck.group(1))
        self.assertEqual(command[:2], ["python", "-c"])

        class Health(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/healthz" else 404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def run(port_value):
            env = {**os.environ, "PORT": str(port_value)}
            return subprocess.run([sys.executable, "-c", command[2]], env=env, capture_output=True, timeout=30).returncode

        self.assertEqual(run(port), 0)
        self.assertNotEqual(run(port + 1 if port < 65535 else port - 1), 0)
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn(':${ARXIV_PORT:-8765}:${ARXIV_PORT:-8765}"', compose)
        self.assertIn('PORT: "${ARXIV_PORT:-8765}"', compose)

    def test_live_code_has_no_ties_to_the_archive(self):
        archived_only = stems(ARCHIVE) - stems(ROOT)
        self.assertIn("bot_server", archived_only)
        live = [*ROOT.glob("*.py"), *(ROOT / "scripts").glob("*.py"),
                *(path for path in (ROOT / "tests").glob("*.py") if path.name != Path(__file__).name)]
        for path in live:
            self.assertFalse(imported_modules(path) & (archived_only | {"archive"}),
                             f"{path.name} imports archived code")
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"\barchive\b",
                                f"{path.name} refers to archive/")

    def test_the_archive_imports_no_live_code(self):
        live_only = stems(ROOT) - stems(ARCHIVE)
        self.assertIn("web_app", live_only)
        for path in ARCHIVE.glob("*.py"):
            self.assertFalse(imported_modules(path) & live_only, f"archive/{path.name} imports live code")

    def test_the_archived_scripts_load_only_archived_code(self):
        # Import them the way `python archive/<script>.py` would, in a clean interpreter,
        # and list every loaded module that lives in the repository but outside archive/.
        probe = (
            "import sys, pathlib\n"
            "archive = pathlib.Path(sys.argv[1]).resolve()\n"
            "sys.path.insert(0, str(archive))\n"
            "import bot_server, slack_bot_test, message_retrieve_test\n"
            "root = archive.parent\n"
            "files = (pathlib.Path(m.__file__).resolve() for m in list(sys.modules.values()) if getattr(m, '__file__', None))\n"
            "leaks = sorted(str(f) for f in files if f.is_relative_to(root) and not f.is_relative_to(archive)\n"
            "               and 'site-packages' not in f.parts)\n"
            "print(leaks)\n"
            "sys.exit(1 if leaks else 0)\n"
        )
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(ARCHIVE)], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
