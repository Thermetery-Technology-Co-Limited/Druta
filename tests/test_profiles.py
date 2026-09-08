"""Every shipped I2C profile loads and obeys the guards.

Runs without a GPU and without NvAPI, so CI and a reviewer on any machine get
the same answer. This is the check a profile PR has to pass before anyone
reaches for hardware.

Validation goes THROUGH railctl.Profile rather than re-implementing the rules.
A second opinion about what a valid profile looks like is how the two drift
apart, and then a file passes review and is refused at runtime.
"""
import glob
import os
import tomllib
import unittest

from druta import railctl

HERE = os.path.dirname(os.path.abspath(__file__))
I2C_DIR = os.path.join(HERE, '..', 'i2c')
TEMPLATE = os.path.join(I2C_DIR, "TEMPLATE.toml")


def load(path):
    with open(path, "rb") as fh:
        return railctl.Profile(tomllib.load(fh), path)


def profile_paths():
    """Every shipped profile. The template is not one - it is deliberately
    incomplete and must fail the same checks real profiles pass."""
    return [p for p in sorted(glob.glob(os.path.join(I2C_DIR, "*.toml")))
            if os.path.abspath(p) != os.path.abspath(TEMPLATE)]


class ProfileTests(unittest.TestCase):

    def test_at_least_one_profile_ships(self):
        self.assertTrue(profile_paths(), "no profiles found in i2c/")

    def test_every_profile_loads(self):
        for path in profile_paths():
            with self.subTest(profile=os.path.basename(path)):
                load(path)

    def test_no_profile_can_unlock_a_denied_register(self):
        """The built-in denylist is additive-only.

        A profile may add hazards. It may never remove one, so a file that
        lists a STORE_* command as writable has to be refused rather than
        quietly honoured.
        """
        for path in profile_paths():
            with self.subTest(profile=os.path.basename(path)):
                prof = load(path)
                for reg in railctl.NEVER_WRITE:
                    self.assertIn(reg, prof.never)
                    self.assertNotIn(reg, prof.writable)

    def test_limits_are_ordered_and_bounded(self):
        for path in profile_paths():
            with self.subTest(profile=os.path.basename(path)):
                p = load(path)
                self.assertLess(p.env_min, p.env_max)
                self.assertLess(p.plausible[0], p.plausible[1])
                # The ceiling has to sit inside the range the rail is even
                # believed to reach, or it bounds nothing.
                self.assertLessEqual(p.ceiling, p.sanity_rail)

    def test_write_range_is_representable(self):
        """A writable profile states the PART's range, not the field's.

        Past the documented range the value can wrap through the sign bit and
        move the rail the wrong way, which is the failure this bound exists to
        stop. Read-only profiles are skipped: they have no range to state.
        """
        for path in profile_paths():
            with self.subTest(profile=os.path.basename(path)):
                p = load(path)
                if p.read_only:
                    continue
                self.assertLessEqual(p.raw_min, p.raw_max)
                self.assertIn(p.wreg, p.writable)
                self.assertNotIn(p.wreg, railctl.NEVER_WRITE)

    def test_provenance_is_filled_in(self):
        """Every provenance line must say something.

        Blank or TODO means the author has not answered "how do you know?",
        which is the whole point of the section.
        """
        for path in profile_paths():
            with self.subTest(profile=os.path.basename(path)):
                prof = load(path)
                self.assertTrue(prof.provenance,
                                "a profile needs a [provenance] section")
                for key, val in prof.provenance.items():
                    self.assertTrue(str(val).strip(),
                                    f"provenance.{key} is empty")
                    self.assertNotIn("TODO", str(val).upper(),
                                     f"provenance.{key} still says TODO")

    def test_no_todo_markers_survive(self):
        for path in profile_paths():
            with self.subTest(profile=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    body = fh.read()
                self.assertNotIn("TODO", body,
                                 "a TODO left in a profile is a profile that "
                                 "is not finished")

    def test_template_exists_but_is_never_loaded_as_a_profile(self):
        """The template must exist, and must never reach a real bus.

        It is deliberately loadable so a contributor can check their copy
        before filling it in, which means the loader - not the parser - is what
        keeps it out. Its identity read is a blank at address 0x00; running
        that against hardware is exactly the accident this guards.
        """
        self.assertTrue(os.path.exists(TEMPLATE), "i2c/TEMPLATE.toml missing")
        loaded = {os.path.basename(p.path) for p in railctl.load_profiles()}
        self.assertNotIn("TEMPLATE.toml", loaded)

    def test_shipped_profiles_are_all_discovered(self):
        """Whatever the loader skips, it must not be a real profile."""
        loaded = {os.path.basename(p.path) for p in railctl.load_profiles()}
        for path in profile_paths():
            self.assertIn(os.path.basename(path), loaded)


class ReadOnlyProfileTests(unittest.TestCase):
    """A profile with no [[write]] is legal, and must be inert.

    This is the first rung for a contributor, so the guarantee it carries has
    to be tested rather than asserted in a comment: it cannot write, because
    there is no register for it to write.
    """

    MINIMAL = {
        "profile": {"format": 1, "name": "read-only test", "rail": "NVVDD"},
        "provenance": {"datasheet": "test"},
        "bus": {"port": 1, "addr7": 0x20},
        "identity": [{"reg": 0xBE, "bytes": 1, "equals": 0xA0}],
        "telemetry": [{"key": "vout_mv", "reg": 0x8B, "bytes": 2,
                       "encoding": "uint"}],
    }

    def test_loads_without_a_write_section(self):
        p = railctl.Profile(dict(self.MINIMAL), "<test>")
        self.assertTrue(p.read_only)
        self.assertEqual(p.writable, set())

    def test_reports_no_hardware_range(self):
        p = railctl.Profile(dict(self.MINIMAL), "<test>")
        self.assertIsNone(p.hw_min_mv)
        self.assertIsNone(p.hw_max_mv)

    def test_still_requires_identity_and_vout(self):
        for drop in ("identity", "telemetry"):
            d = {k: v for k, v in self.MINIMAL.items() if k != drop}
            with self.subTest(missing=drop):
                with self.assertRaises(railctl.ProfileError):
                    railctl.Profile(d, "<test>")


if __name__ == "__main__":
    unittest.main()
