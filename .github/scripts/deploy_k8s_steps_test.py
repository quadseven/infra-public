#!/usr/bin/env python3
"""Runs the real guard steps of deploy.k8s.yml under bash (infra-public#139).

deploy.k8s.yml cannot run end to end here: it needs a cluster and a
registry. Two of its steps are plain bash that decide whether a deploy is
refused or ships the right image, and those can run anywhere:

- `Preflight - required inputs present`
- `Pin image placeholders`

Each test lifts the step's `run:` script out of the workflow file as it is
on disk and runs it with the env that step declares, so a change to the
step is tested as written rather than through a copy. Same approach as
close-completeness/workflow.test.js for the close-completeness reusable.

Python stdlib only (unittest), plus bash.

Run: python3 .github/scripts/deploy_k8s_steps_test.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "deploy.k8s.yml"
STEP_RE = re.compile(r"^(\s*)- name:\s*(.+?)\s*$")


def step_script(name: str, text: str | None = None) -> str:
    """The dedented `run: |` block of the step called `name`."""
    lines = (text if text is not None else WORKFLOW.read_text()).splitlines()
    for i, line in enumerate(lines):
        m = STEP_RE.match(line)
        if not m or m.group(2).strip("\"'") != name:
            continue
        step_indent = len(m.group(1))
        for j in range(i + 1, len(lines)):
            cur = lines[j]
            if cur.strip() and len(cur) - len(cur.lstrip()) <= step_indent:
                break
            if re.match(r"^\s*run:\s*\|\s*$", cur):
                key_indent = len(cur) - len(cur.lstrip())
                body = []
                for k in range(j + 1, len(lines)):
                    nxt = lines[k]
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= key_indent:
                        break
                    body.append(nxt)
                indent = min(len(b) - len(b.lstrip()) for b in body if b.strip())
                return "\n".join(b[indent:] for b in body) + "\n"
        raise AssertionError(f"step {name!r} has no `run: |` block")
    raise AssertionError(f"no step named {name!r} in {WORKFLOW.name}")


def _gnu_sed_path() -> str | None:
    """A PATH on which `sed` is GNU sed, as on the Linux runner the step
    targets (`sed -i` without a suffix argument is GNU syntax). None if this
    machine has no GNU sed."""
    path = os.environ.get("PATH", "/usr/bin:/bin")
    probe = subprocess.run(["sed", "--version"], capture_output=True, text=True, check=False)
    if probe.returncode == 0 and "GNU" in probe.stdout:
        return path
    gsed = shutil.which("gsed")
    if gsed is None:
        return None
    shim = Path(tempfile.mkdtemp(prefix="gnu-sed-"))
    (shim / "sed").symlink_to(gsed)
    return f"{shim}{os.pathsep}{path}"


GNU_SED_PATH = _gnu_sed_path()


def run(script: str, env: dict[str, str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    base = {"PATH": GNU_SED_PATH or os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(
        ["bash", "-e", "-c", script],
        env={**base, **env},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


PREFLIGHT = "Preflight - required inputs present"
PIN = "Pin image placeholders"


class Preflight(unittest.TestCase):
    def preflight(self, **env: str) -> subprocess.CompletedProcess[str]:
        defaults = {
            "REGISTRY_HOST": "registry.example.com",
            "CLUSTER_ACCESS": "tailnet",
            "HAS_TS_AUTHKEY": "true",
            "HAS_KUBECONFIG": "true",
        }
        return run(step_script(PREFLIGHT), {**defaults, **env})

    def assert_refused(self, done: subprocess.CompletedProcess[str], why: str) -> None:
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn(why, done.stdout)

    def test_tailnet_with_both_secrets_passes(self):
        self.assertEqual(self.preflight().returncode, 0)

    def test_an_empty_registry_host_is_refused(self):
        self.assert_refused(self.preflight(REGISTRY_HOST=""), "registry-host is empty")

    def test_tailnet_without_the_auth_key_is_refused(self):
        self.assert_refused(self.preflight(HAS_TS_AUTHKEY="false"), "ts-authkey secret is empty")

    def test_tailnet_without_the_kubeconfig_is_refused(self):
        self.assert_refused(self.preflight(HAS_KUBECONFIG="false"), "kubeconfig-b64 secret is empty")

    def test_in_cluster_without_a_kubeconfig_passes(self):
        done = self.preflight(CLUSTER_ACCESS="in-cluster", HAS_TS_AUTHKEY="false", HAS_KUBECONFIG="false")
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_in_cluster_with_a_kubeconfig_is_refused(self):
        self.assert_refused(
            self.preflight(CLUSTER_ACCESS="in-cluster", HAS_KUBECONFIG="true"),
            "cluster-access is in-cluster but a kubeconfig-b64 secret was passed",
        )

    def test_an_unknown_mode_is_refused(self):
        self.assert_refused(self.preflight(CLUSTER_ACCESS="vpn"), "must be 'tailnet' or 'in-cluster', got 'vpn'")


MANIFEST = "image: REGISTRY_PLACEHOLDER/app:TAG_PLACEHOLDER\n"
SHA = "0123456789abcdef0123456789abcdef01234567"


# Never skipped in CI (CI=true on every Actions runner): a skip there would
# report green over tests that did not run.
@unittest.skipIf(
    GNU_SED_PATH is None and not os.environ.get("CI"),
    "needs GNU sed, as on the Linux runner (brew install gnu-sed)",
)
class PinPlaceholders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "k8s").mkdir()
        (self.root / "k8s" / "deployment.yaml").write_text(MANIFEST)
        (self.root / "k8s" / "service.yaml").write_text("kind: Service\n")
        (self.root / "migrate.yaml").write_text(MANIFEST)

    def tearDown(self):
        self.tmp.cleanup()

    def pin(self, migrate: str = "") -> subprocess.CompletedProcess[str]:
        env = {
            "REGISTRY_HOST": "registry.example.com",
            "KUSTOMIZE_DIR": "k8s",
            "MIGRATE_MANIFEST": migrate,
            "GITHUB_SHA": SHA,
        }
        return run(step_script(PIN), env, cwd=self.tmp.name)

    def test_the_kustomize_dir_is_pinned_to_the_registry_and_commit(self):
        done = self.pin()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            (self.root / "k8s" / "deployment.yaml").read_text(),
            f"image: registry.example.com/app:{SHA}\n",
        )
        # Without a migrate manifest, nothing outside the dir is touched.
        self.assertEqual((self.root / "migrate.yaml").read_text(), MANIFEST)

    def test_a_migrate_manifest_is_pinned_too(self):
        done = self.pin(migrate="migrate.yaml")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.root / "migrate.yaml").read_text(), f"image: registry.example.com/app:{SHA}\n")

    def test_a_surviving_placeholder_fails_the_step(self):
        # Only the sentinel can see this: a placeholder sed does not rewrite.
        (self.root / "k8s" / "deployment.yaml").write_text("image: x/app:TAG_PLACEHOLDER\n")
        script = step_script(PIN).replace('-e "s|TAG_PLACEHOLDER|${GITHUB_SHA}|g"', "")
        done = run(script, {"REGISTRY_HOST": "r", "KUSTOMIZE_DIR": "k8s", "MIGRATE_MANIFEST": "", "GITHUB_SHA": SHA},
                   cwd=self.tmp.name)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("unsubstituted placeholder after pin", done.stdout)


class Extraction(unittest.TestCase):
    def test_a_missing_step_is_an_error_not_an_empty_script(self):
        with self.assertRaises(AssertionError):
            step_script("No such step")


if __name__ == "__main__":
    unittest.main(verbosity=2)
