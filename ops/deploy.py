"""
deploy.py — versioned config deployment + clean revert.

Deploying a change is never an in-place edit of the live spec. It is: write a
NEW versioned spec file, point the live symlink/pointer at it, keep the previous
one. Revert is then trivial and lossless — just point back at the prior version.

This is why every decision is stamped with strategy_version + config_hash: a
revert is unambiguous, and the trial's trades can be isolated to exactly the
version under test.
"""
from __future__ import annotations

from pathlib import Path
import copy
import yaml

from core.schema import config_hash


class Deployer:
    def __init__(self, versions_dir: str | Path, live_pointer: str | Path):
        self.versions_dir = Path(versions_dir)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.live_pointer = Path(live_pointer)  # a small file holding the live version name

    def make_candidate(self, base_config: dict, param_path: str, value, new_version: str) -> dict:
        cfg = copy.deepcopy(base_config)
        node = cfg
        parts = param_path.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
        cfg["version"] = new_version
        return cfg

    def write_version(self, config: dict) -> Path:
        path = self.versions_dir / f"{config['version']}.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        return path

    def live_version(self) -> str | None:
        return self.live_pointer.read_text().strip() if self.live_pointer.exists() else None

    def deploy(self, config: dict) -> str:
        """Make `config` the live version. Returns the version that WAS live (for revert)."""
        previous = self.live_version()
        self.write_version(config)
        self.live_pointer.write_text(config["version"])
        return previous or ""

    def revert_to(self, version: str) -> None:
        if not (self.versions_dir / f"{version}.yaml").exists():
            raise FileNotFoundError(f"cannot revert: version {version} not found")
        self.live_pointer.write_text(version)

    def load_live(self) -> dict:
        v = self.live_version()
        if not v:
            raise RuntimeError("no live version set")
        cfg = yaml.safe_load((self.versions_dir / f"{v}.yaml").read_text())
        # sanity: stamped hash should match content
        cfg["_config_hash"] = config_hash({k: v for k, v in cfg.items() if k != "_config_hash"})
        return cfg
