import tempfile
import unittest
from pathlib import Path

from tools import updater


class UpdaterProtectionTests(unittest.TestCase):
    def test_authorization_files_are_protected_user_data(self):
        protected = (
            "auth_state.dat",
            "auth_device.json",
            "auth_state.dat.tmp",
            "auth_device.json.tmp",
            "config.json",
            "records.json",
        )
        for name in protected:
            with self.subTest(name=name):
                self.assertTrue(updater.is_protected(Path(name)))

    def test_apply_payload_does_not_overwrite_authorization_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload"
            app_dir = root / "app"
            payload.mkdir()
            app_dir.mkdir()
            for name in ("auth_state.dat", "auth_device.json"):
                (payload / name).write_text(f"new {name}", encoding="utf-8")
                (app_dir / name).write_text(f"existing {name}", encoding="utf-8")
            (payload / "YHoAutoFish.exe").write_text("new exe", encoding="utf-8")

            copied, skipped = updater.apply_payload(payload, app_dir)

            self.assertEqual(1, copied)
            self.assertEqual(2, skipped)
            self.assertEqual("existing auth_state.dat", (app_dir / "auth_state.dat").read_text(encoding="utf-8"))
            self.assertEqual("existing auth_device.json", (app_dir / "auth_device.json").read_text(encoding="utf-8"))
            self.assertEqual("new exe", (app_dir / "YHoAutoFish.exe").read_text(encoding="utf-8"))

    def test_build_script_rejects_auth_cache_files_in_release_payload(self):
        source = Path("build_release.ps1").read_text(encoding="utf-8")

        self.assertIn("auth_state.dat", source)
        self.assertIn("auth_device.json", source)
        self.assertIn("OpenRead($ArchivePath)", source)
        self.assertIn("Forbidden release payload file", source)


if __name__ == "__main__":
    unittest.main()
