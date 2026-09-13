"""Immutable Git snapshots and compare-before-push image-only commits."""

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ruamel.yaml import YAML

from .common import Error, atomic_write, private_dir, run

GIT = [
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "protocol.file.allow=always",
]


class Source:
    def __init__(self, state, cfg):
        self.cfg = cfg
        key = hashlib.sha256(cfg["repository"].encode()).hexdigest()[:24]
        self.root = private_dir(state / "sources" / key)
        self.git = self.root / "cache.git"

    def fetch(self):
        if not self.git.exists():
            run([*GIT, "init", "--bare", str(self.git)])
        run(
            [
                *GIT,
                "--git-dir",
                str(self.git),
                "fetch",
                "--no-tags",
                "--",
                self.cfg["repository"],
                self.cfg["ref"],
            ]
        )
        revision = run([*GIT, "--git-dir", str(self.git), "rev-parse", "FETCH_HEAD^{commit}"]).strip()
        return self.snapshot(revision)

    def snapshot(self, revision):
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise Error("invalid_revision", "An exact Git commit is required")
        destination = self.root / revision
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(dir=self.root))
            try:
                # Archive into a bounded file: avoid checkout filters and repository hooks.
                with tempfile.TemporaryFile() as archive:
                    result = subprocess.run(
                        [*GIT, "--git-dir", str(self.git), "archive", revision],
                        stdout=archive,
                        stderr=subprocess.DEVNULL,
                        timeout=120,
                        check=False,
                    )
                    if result.returncode or archive.tell() > 128 * 1024**2:
                        raise Error("source_archive", "Git archive failed or exceeds 128 MiB")
                    archive.seek(0)
                    with tarfile.open(fileobj=archive) as bundle:
                        for member in bundle.getmembers():
                            path = Path(member.name)
                            if (
                                not (member.isdir() or member.isfile())
                                or path.is_absolute()
                                or ".." in path.parts
                            ):
                                raise Error(
                                    "unsafe_source", "Git source must contain regular files/directories only"
                                )
                        bundle.extractall(staging, filter="data")
                staging.rename(destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        compose = destination / self.cfg["compose_file"]
        if not compose.is_file():
            raise Error("missing_compose", "Configured Compose file is missing from the revision")
        return revision, compose

    def commit_image(self, revision, expected_image, new_image):
        """Publish one targeted change. Ordinary push rejects concurrent branch changes."""
        current, _ = self.fetch()
        if current != revision:
            raise Error("source_changed", "Branch advanced after update planning; plan again", retryable=True)
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            run([*GIT, "clone", "--no-checkout", "--", str(self.git), str(root)])
            run([*GIT, "checkout", "--detach", revision], cwd=root)
            path = root / self.cfg["compose_file"]
            yaml = YAML()
            yaml.preserve_quotes = True
            document = yaml.load(path.read_text())
            service = document["services"][self.cfg["service"]]
            if service.get("image") != expected_image:
                raise Error(
                    "image_expression", "Automatic updates require a literal matching image reference"
                )
            before = json.loads(json.dumps(document))
            service["image"] = new_image
            changed = io.StringIO()
            yaml.dump(document, changed)
            check = yaml.load(changed.getvalue())
            check["services"][self.cfg["service"]]["image"] = expected_image
            if check != before:
                raise Error("unsafe_diff", "Version update would change more than the selected image")
            atomic_write(path, changed.getvalue())
            run([*GIT, "add", "--", self.cfg["compose_file"]], cwd=root)
            run(
                [
                    *GIT,
                    "-c",
                    "user.name=Mooring",
                    "-c",
                    "user.email=mooring@localhost",
                    "commit",
                    "-m",
                    f"Update {self.cfg['name']} to {new_image}",
                ],
                cwd=root,
            )
            commit = run([*GIT, "rev-parse", "HEAD"], cwd=root).strip()
            run([*GIT, "push", "--", self.cfg["repository"], f"HEAD:{self.cfg['ref']}"], cwd=root)
            return commit
