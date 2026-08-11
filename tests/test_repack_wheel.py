import hashlib
import importlib.util
import io
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sasori_repack_wheel", ROOT / "scripts" / "repack_wheel.py"
)
assert SPEC is not None and SPEC.loader is not None
repack_wheel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repack_wheel
SPEC.loader.exec_module(repack_wheel)


def member(name: str, content: bytes, *, mode: int = 0o644) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, (2026, 1, 2, 3, 4, 6))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info, content


class WheelRepackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wheel = self.root / "sasori-0-py3-none-any.whl"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, entries, *, comment=b""):
        with zipfile.ZipFile(
            self.wheel, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for info, content in entries:
                archive.writestr(
                    info,
                    content,
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                )
            archive.comment = comment

    @staticmethod
    def python_source() -> bytes:
        return "".join(
            f"def handler_{index}(request):\n"
            f"    value = request.get('field_{index % 47}')\n"
            f"    return {{'sequence': {index}, 'value': value}}\n"
            for index in range(1800)
        ).encode("utf-8")

    def test_repack_preserves_payload_metadata_and_is_byte_idempotent(self):
        entries = (
            member("sasori/module.py", self.python_source(), mode=0o755),
            member("sasori-0.dist-info/WHEEL", b"Wheel-Version: 1.0\n"),
            member("sasori-0.dist-info/RECORD", b"x"),
        )
        self.write(entries)
        expected = {info.filename: content for info, content in entries}

        evidence = repack_wheel.repack(self.wheel)
        first = self.wheel.read_bytes()
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            infos = archive.infolist()
            self.assertEqual(
                {info.filename: archive.read(info) for info in infos}, expected
            )
            self.assertEqual(infos[0].date_time, (2026, 1, 2, 3, 4, 6))
            self.assertEqual((infos[0].external_attr >> 16) & 0o777, 0o755)
            self.assertEqual(
                {info.compress_type for info in infos},
                {zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2},
            )
            self.assertTrue(all(not (info.flag_bits & 0x9) for info in infos))
        self.assertEqual(evidence["members"], 3)
        self.assertGreater(evidence["methods"]["bzip2"], 0)
        self.assertGreater(evidence["methods"]["deflate"], 0)
        self.assertEqual(evidence["sha256"], hashlib.sha256(first).hexdigest())

        second_evidence = repack_wheel.repack(self.wheel)
        self.assertEqual(self.wheel.read_bytes(), first)
        self.assertEqual(second_evidence["before_bytes"], len(first))
        self.assertEqual(second_evidence["after_bytes"], len(first))

    def test_invalid_archive_is_rejected_without_replacing_the_input(self):
        cases = (
            ((member("../escape.py", b"x"),), b""),
            (
                (
                    member("sasori/Case.py", b"one"),
                    member("sasori/case.py", b"two"),
                ),
                b"",
            ),
            ((member("sasori/module.py", b"x"),), b"archive comment"),
        )
        for entries, comment in cases:
            with self.subTest(entries=[entry[0].filename for entry in entries]):
                self.write(entries, comment=comment)
                before = self.wheel.read_bytes()
                with self.assertRaises(repack_wheel.WheelRepackError):
                    repack_wheel.repack(self.wheel)
                self.assertEqual(self.wheel.read_bytes(), before)

    def test_symlink_member_is_rejected(self):
        info = zipfile.ZipInfo("sasori/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.write(((info, b"target"),))
        with self.assertRaisesRegex(
            repack_wheel.WheelRepackError, "member contract"
        ):
            repack_wheel.repack(self.wheel)


if __name__ == "__main__":
    unittest.main()
