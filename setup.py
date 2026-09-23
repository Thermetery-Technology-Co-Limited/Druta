# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Copy canonical repository assets into installed wheels without source duplicates."""
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def asset_mapping(self):
        assets = [Path("COPYING"), Path("THIRD-PARTY-NOTICES.md")]
        assets += sorted(Path("i2c").glob("*.toml"))
        assets += sorted(Path("i2c").glob("*.md"))
        target = Path(self.build_lib) / "druta" / "_data"
        return {str(target / source): str(source) for source in assets}

    def run(self):
        super().run()
        # Editable installs resolve canonical files directly from the checkout.
        if self.editable_mode:
            return
        for destination, source in self.asset_mapping().items():
            self.mkpath(str(Path(destination).parent))
            self.copy_file(source, destination)

    def get_outputs(self, include_bytecode=1):
        return super().get_outputs(include_bytecode) + list(self.asset_mapping())

    def get_output_mapping(self):
        return {**super().get_output_mapping(), **self.asset_mapping()}

    def get_source_files(self):
        return super().get_source_files() + list(self.asset_mapping().values())


setup(cmdclass={"build_py": BuildPy})
