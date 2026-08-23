import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.terminal_bench.codex_runtime import (
    CODEX_ARCHIVE_ENV,
    CODEX_RUNTIME_ENTRYPOINT,
    CODEX_RUNTIME_INSTALL_ROOT,
    CODEX_RUNTIME_PREPARED_RELATIVE,
    CODEX_RUNTIME_SPEC_SHA256,
    HARBOR_CODEX_EXEC_PREFIX,
    _verify_regular_file,
    build_full_tree_verification_command,
    codex_runtime_spec,
    prepare_tree,
    rewrite_harbor_launch,
    validate_codex_runtime_spec,
    verify_host_archive,
    verify_tree,
)


def _tiny_spec(archive: Path, files: dict[str, bytes]) -> dict[str, object]:
    spec = codex_runtime_spec()
    spec["entrypoint"] = f"{CODEX_RUNTIME_INSTALL_ROOT}/{next(iter(files))}"
    spec["files"] = [
        {
            "path": path,
            "mode": "0644",
            "bytes": len(content),
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
        for path, content in files.items()
    ]
    spec["archive"] = {
        "url": "https://invalid.example/frozen.tgz",
        "bytes": archive.stat().st_size,
        "sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
        "root": "package",
    }
    return spec


def _write_tar(
    archive: Path,
    members: list[tuple[str, bytes | None]],
    *,
    mode: int = 0o644,
) -> None:
    with tarfile.open(archive, "w:gz") as bundle:
        for name, content in members:
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.mtime = 0
            if content is None:
                member.type = tarfile.SYMTYPE
                member.linkname = "/outside"
                bundle.addfile(member)
            else:
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))


class CodexRuntimeTest(unittest.TestCase):
    def test_frozen_literals_and_canonical_digest(self) -> None:
        self.assertEqual(CODEX_ARCHIVE_ENV, "OPEN_AGENT_LAB_CODEX_ARCHIVE")
        self.assertEqual(
            CODEX_RUNTIME_INSTALL_ROOT,
            "/installed-agent/open-agent-lab/codex-0.149.0-x86_64-unknown-linux-musl",
        )
        self.assertEqual(
            CODEX_RUNTIME_ENTRYPOINT,
            CODEX_RUNTIME_INSTALL_ROOT + "/vendor/x86_64-unknown-linux-musl/bin/codex",
        )
        self.assertEqual(
            CODEX_RUNTIME_PREPARED_RELATIVE,
            "codex-runtime/codex-0.149.0-x86_64-unknown-linux-musl",
        )
        canonical = json.dumps(
            codex_runtime_spec(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self.assertEqual(
            CODEX_RUNTIME_SPEC_SHA256,
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

    def test_spec_is_fresh_and_exact(self) -> None:
        first = codex_runtime_spec()
        second = codex_runtime_spec()
        self.assertEqual(validate_codex_runtime_spec(first), second)
        self.assertIsNot(first, second)
        self.assertIsNot(first["archive"], second["archive"])
        self.assertIsNot(first["files"], second["files"])

    def test_spec_rejects_schema_value_order_and_type_mutations(self) -> None:
        mutations = []
        extra = codex_runtime_spec()
        extra["extra"] = None
        mutations.append(extra)
        wrong_version = codex_runtime_spec()
        wrong_version["version"] = "0.149.1"
        mutations.append(wrong_version)
        wrong_bool = codex_runtime_spec()
        wrong_bool["schemaVersion"] = True
        mutations.append(wrong_bool)
        wrong_archive_bool = codex_runtime_spec()
        wrong_archive_bool["archive"]["bytes"] = True  # type: ignore[index]
        mutations.append(wrong_archive_bool)
        reordered = codex_runtime_spec()
        reordered["files"][0], reordered["files"][1] = (  # type: ignore[index]
            reordered["files"][1],  # type: ignore[index]
            reordered["files"][0],  # type: ignore[index]
        )
        mutations.append(reordered)

        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_codex_runtime_spec(mutation)

    def test_spec_rejects_unsafe_paths_before_pin_comparison(self) -> None:
        for bad_path in ("/README.md", "../README.md", "a/../README.md", "./README.md"):
            spec = codex_runtime_spec()
            spec["files"][0]["path"] = bad_path  # type: ignore[index]
            with (
                self.subTest(path=bad_path),
                self.assertRaisesRegex(ValueError, "canonical relative"),
            ):
                validate_codex_runtime_spec(spec)

        for key, value in (
            ("installRoot", "installed-agent/codex"),
            ("entrypoint", "/outside/codex"),
        ):
            spec = codex_runtime_spec()
            spec[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_codex_runtime_spec(spec)

        spec = codex_runtime_spec()
        spec["archive"]["root"] = "../package"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "canonical relative"):
            validate_codex_runtime_spec(spec)

    def test_archive_file_requires_absolute_regular_exact_bytes(self) -> None:
        content = b"frozen-codex-archive"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "codex.tgz"
            archive.write_bytes(content)
            _verify_regular_file(archive, len(content), digest)

            with self.assertRaisesRegex(ValueError, "absolute"):
                _verify_regular_file(Path("codex.tgz"), len(content), digest)
            with self.assertRaisesRegex(ValueError, "size"):
                _verify_regular_file(archive, len(content) + 1, digest)
            with self.assertRaisesRegex(ValueError, "hash"):
                _verify_regular_file(archive, len(content), "0" * 64)

            link = Path(directory) / "link.tgz"
            link.symlink_to(archive)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                _verify_regular_file(link, len(content), digest)

            folder = Path(directory) / "folder"
            folder.mkdir()
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                _verify_regular_file(folder, 0, hashlib.sha256(b"").hexdigest())

    def test_archive_receipt_is_stable_and_excludes_host_path(self) -> None:
        with patch(
            "benchmarks.terminal_bench.codex_runtime._verify_regular_file"
        ) as verify:
            receipt = verify_host_archive(codex_runtime_spec(), "/host/private.tgz")
        verify.assert_called_once()
        self.assertEqual(
            receipt,
            {
                "schema_version": 1,
                "spec_sha256": CODEX_RUNTIME_SPEC_SHA256,
                "version": "0.149.0",
                "platform": "linux/amd64",
                "archive_sha256": "sha256:"
                "e06f3d106fe8bb058a6bfd30075d89ea17deaee7c8425e0c5d23072df0fdd0e7",
                "entrypoint_sha256": "sha256:"
                "bbc3341e44c9ead340ed9570c17be936e37870f570751a941699ffd04d672827",
            },
        )
        self.assertNotIn("/host/private.tgz", json.dumps(receipt))

    def test_runtime_verification_command_pins_every_file(self) -> None:
        spec = codex_runtime_spec()
        verify = build_full_tree_verification_command(spec)

        for entry in spec["files"]:  # type: ignore[union-attr]
            for key in ("path", "bytes"):
                self.assertIn(str(entry[key]), verify)
            self.assertIn(str(entry["sha256"]).removeprefix("sha256:"), verify)

        self.assertNotIn("npm", verify)
        self.assertNotIn("NVM", verify)
        self.assertNotIn("PATH", verify)
        self.assertIn("stat -c %h", verify)

    def test_prepare_and_verify_tree_stream_exact_regular_members(self) -> None:
        files = {"a.txt": b"alpha", "nested/b.bin": b"beta"}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "runtime.tgz"
            _write_tar(
                archive,
                [(f"package/{path}", content) for path, content in files.items()],
            )
            spec = _tiny_spec(archive, files)
            root = base / "prepared"
            with (
                patch(
                    "benchmarks.terminal_bench.codex_runtime."
                    "validate_codex_runtime_spec",
                    return_value=spec,
                ),
                patch(
                    "benchmarks.terminal_bench.codex_runtime.tarfile.open",
                    wraps=tarfile.open,
                ) as open_archive,
            ):
                receipt = prepare_tree(archive, root, spec)
                self.assertEqual(receipt, verify_tree(root, spec))
            self.assertEqual(open_archive.call_args.kwargs["mode"], "r|gz")
            self.assertEqual(
                {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                set(files),
            )
            for path, content in files.items():
                self.assertEqual((root / path).read_bytes(), content)
                self.assertEqual((root / path).stat().st_mode & 0o777, 0o644)

    def test_prepare_rejects_member_and_byte_drift_and_cleans_up(self) -> None:
        files = {"a.txt": b"alpha", "nested/b.bin": b"beta"}
        valid = [(f"package/{path}", content) for path, content in files.items()]
        mutations = {
            "traversal": (valid + [("package/../escape", b"escape")], 0o644),
            "link": ([("package/a.txt", None), valid[1]], 0o644),
            "extra": (valid + [("package/extra", b"extra")], 0o644),
            "duplicate": (valid + [valid[0]], 0o644),
            "missing": (valid[:1], 0o644),
            "size": ([("package/a.txt", b"alpha!"), valid[1]], 0o644),
            "hash": ([("package/a.txt", b"ALPHA"), valid[1]], 0o644),
            "mode": (valid, 0o600),
        }
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, (members, mode) in mutations.items():
                archive = base / f"{name}.tgz"
                _write_tar(archive, members, mode=mode)
                spec = _tiny_spec(archive, files)
                destination = base / name
                with (
                    self.subTest(mutation=name),
                    patch(
                        "benchmarks.terminal_bench.codex_runtime."
                        "validate_codex_runtime_spec",
                        return_value=spec,
                    ),
                    self.assertRaises(ValueError),
                ):
                    prepare_tree(archive, destination, spec)
                self.assertFalse(destination.exists())
            self.assertFalse((base / "escape").exists())

    def test_harbor_launch_rewrite_is_exact_and_path_free(self) -> None:
        original = HARBOR_CODEX_EXEC_PREFIX + "--json 'solve'"
        rewritten = rewrite_harbor_launch(original)
        self.assertEqual(
            rewritten,
            f"{CODEX_RUNTIME_ENTRYPOINT} exec --json 'solve'",
        )
        self.assertNotIn("nvm", rewritten.lower())
        self.assertNotIn("PATH", rewritten)

        for command in (
            "codex exec --json solve",
            "prefix " + original,
            HARBOR_CODEX_EXEC_PREFIX.replace("codex exec ", "codex exec  ") + "solve",
            1,
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                rewrite_harbor_launch(command)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
