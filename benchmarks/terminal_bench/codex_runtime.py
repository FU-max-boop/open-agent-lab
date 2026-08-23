"""Frozen native Codex runtime boundary for the Terminal-Bench pilot."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

CODEX_ARCHIVE_ENV = "OPEN_AGENT_LAB_CODEX_ARCHIVE"
CODEX_RUNTIME_INSTALL_ROOT = (
    "/installed-agent/open-agent-lab/codex-0.149.0-x86_64-unknown-linux-musl"
)
CODEX_RUNTIME_PREPARED_RELATIVE = (
    "codex-runtime/codex-0.149.0-x86_64-unknown-linux-musl"
)
CODEX_RUNTIME_ENTRYPOINT = (
    f"{CODEX_RUNTIME_INSTALL_ROOT}/vendor/x86_64-unknown-linux-musl/bin/codex"
)
HARBOR_CODEX_EXEC_PREFIX = (
    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; codex exec "
)

_ARCHIVE_URL = "https://registry.npmjs.org/@openai/codex/-/codex-0.149.0-linux-x64.tgz"
_ARCHIVE_BYTES = 125_570_601
_ARCHIVE_SHA256 = "e06f3d106fe8bb058a6bfd30075d89ea17deaee7c8425e0c5d23072df0fdd0e7"
_FILES = (
    (
        "README.md",
        "0644",
        3_334,
        "ba4e1f69ff48386e72a9c5e1edaf76aad64a475c2d51af79ccba6d1128261ba7",
    ),
    (
        "package.json",
        "0644",
        511,
        "c0c4e6c91afe909fadc61529255d54b831525b43f69fca4766ee862f0c52e1bc",
    ),
    (
        "vendor/x86_64-unknown-linux-musl/bin/codex",
        "0755",
        258_322_048,
        "bbc3341e44c9ead340ed9570c17be936e37870f570751a941699ffd04d672827",
    ),
    (
        "vendor/x86_64-unknown-linux-musl/bin/codex-code-mode-host",
        "0755",
        57_882_552,
        "ba0f620a1d242a7555750e96c30c4c03b8b89ec0d4cff987e21bcd79fa18a363",
    ),
    (
        "vendor/x86_64-unknown-linux-musl/codex-package.json",
        "0644",
        205,
        "0b3f869f9fbd009f4d0c68c68ab50d53778855899b19106a607c6cd1861637a3",
    ),
    (
        "vendor/x86_64-unknown-linux-musl/codex-path/rg",
        "0755",
        5_408_904,
        "e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849",
    ),
    (
        "vendor/x86_64-unknown-linux-musl/codex-resources/bwrap",
        "0755",
        529_776,
        "7df960565a0dece99240ea4b9d0e011307817f9f3b73176c7b71fda44fe84765",
    ),
    (
        "vendor/x86_64-unknown-linux-musl/codex-resources/zsh/bin/zsh",
        "0755",
        898_480,
        "67faaaa89242c4a332e16e508a1977cffc24bf7fca31d4411cdfd101f3831ef3",
    ),
)
# Updated only when the canonical codex_runtime_spec() bytes intentionally change.
CODEX_RUNTIME_SPEC_SHA256 = (
    "sha256:e9c7a81d6ff68915730e704d3ba610cf91058dfaf452b5324195770e98b62e2a"
)


def codex_runtime_spec() -> dict[str, object]:
    """Return a fresh copy of the only accepted runtime specification."""
    return {
        "schemaVersion": 1,
        "platform": "linux/amd64",
        "version": "0.149.0",
        "installRoot": CODEX_RUNTIME_INSTALL_ROOT,
        "entrypoint": CODEX_RUNTIME_ENTRYPOINT,
        "archive": {
            "url": _ARCHIVE_URL,
            "bytes": _ARCHIVE_BYTES,
            "sha256": f"sha256:{_ARCHIVE_SHA256}",
            "root": "package",
        },
        "files": [
            {
                "path": path,
                "mode": mode,
                "bytes": size,
                "sha256": f"sha256:{digest}",
            }
            for path, mode, size, digest in _FILES
        ],
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _require_exact(actual: object, expected: object, at: str) -> None:
    if type(actual) is not type(expected):
        raise ValueError(f"{at} has the wrong type")
    if isinstance(expected, dict):
        if actual.keys() != expected.keys():  # type: ignore[union-attr]
            raise ValueError(f"{at} has the wrong fields")
        for key, value in expected.items():
            _require_exact(actual[key], value, f"{at}.{key}")  # type: ignore[index]
    elif isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise ValueError(f"{at} has the wrong length")
        for index, value in enumerate(expected):
            _require_exact(actual[index], value, f"{at}[{index}]")  # type: ignore[index]
    elif actual != expected:
        raise ValueError(f"{at} drifted")


def _safe_relative(value: object, at: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{at} must be a string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{at} is not a canonical relative path")


def validate_codex_runtime_spec(value: object) -> dict[str, object]:
    """Reject any schema, type, path, order, or pinned-byte drift."""
    if type(value) is not dict:
        raise ValueError("codexRuntime must be an object")
    files = value.get("files")
    archive = value.get("archive")
    if type(files) is not list or type(archive) is not dict:
        raise ValueError("codexRuntime archive/files have the wrong type")
    for index, entry in enumerate(files):
        if type(entry) is not dict:
            raise ValueError(f"codexRuntime.files[{index}] must be an object")
        _safe_relative(entry.get("path"), f"codexRuntime.files[{index}].path")
    _safe_relative(archive.get("root"), "codexRuntime.archive.root")

    install_root = value.get("installRoot")
    entrypoint = value.get("entrypoint")
    if not isinstance(install_root, str) or not isinstance(entrypoint, str):
        raise TypeError("codexRuntime install paths must be strings")
    root = PurePosixPath(install_root)
    executable = PurePosixPath(entrypoint)
    if not root.is_absolute() or root.as_posix() != install_root:
        raise ValueError("codexRuntime.installRoot is not canonical absolute")
    if not executable.is_absolute() or executable.as_posix() != entrypoint:
        raise ValueError("codexRuntime.entrypoint is not canonical absolute")
    if root not in executable.parents:
        raise ValueError("codexRuntime.entrypoint escapes installRoot")

    expected = codex_runtime_spec()
    _require_exact(value, expected, "codexRuntime")
    digest = "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()
    if digest != CODEX_RUNTIME_SPEC_SHA256:
        raise ValueError("codexRuntime canonical digest drifted")
    return expected


@contextmanager
def _verified_regular_file(path: Path, expected_size: int, expected_hash: str):
    if not path.is_absolute():
        raise ValueError("Codex archive path must be absolute")
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Codex archive must be a regular non-symlink file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("Codex archive cannot be opened safely") from error
    stream = os.fdopen(descriptor, "rb")
    try:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ValueError("Codex archive size or file type drifted")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected_hash:
            raise ValueError("Codex archive hash or identity drifted")
        stream.seek(0)
        yield stream
        after = os.fstat(stream.fileno())
        stream.seek(0)
        final_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
        )
        if identity(before) != identity(after) or final_digest != expected_hash:
            raise ValueError("Codex archive changed while in use")
    finally:
        stream.close()


def _verify_regular_file(path: Path, expected_size: int, expected_hash: str) -> None:
    with _verified_regular_file(path, expected_size, expected_hash):
        pass


def verify_host_archive(
    spec: object, archive_path: str | os.PathLike[str]
) -> dict[str, object]:
    """Verify pinned host bytes and return a stable, non-host-specific receipt."""
    validated = validate_codex_runtime_spec(spec)
    archive = validated["archive"]
    assert isinstance(archive, dict)
    _verify_regular_file(
        Path(archive_path),
        archive["bytes"],  # type: ignore[arg-type]
        archive["sha256"].removeprefix("sha256:"),  # type: ignore[union-attr]
    )
    return {
        "schema_version": 1,
        "spec_sha256": CODEX_RUNTIME_SPEC_SHA256,
        "version": validated["version"],
        "platform": validated["platform"],
        "archive_sha256": archive["sha256"],
        "entrypoint_sha256": f"sha256:{_FILES[2][3]}",
    }


def _expected_directories(files: list[dict[str, object]]) -> set[str]:
    return {
        parent.as_posix()
        for entry in files
        for parent in PurePosixPath(entry["path"]).parents  # type: ignore[arg-type]
        if parent.as_posix() != "."
    }


def _tree_receipt(spec: dict[str, object]) -> dict[str, object]:
    files = spec["files"]
    assert isinstance(files, list)
    relative = PurePosixPath(spec["entrypoint"]).relative_to(  # type: ignore[arg-type]
        PurePosixPath(spec["installRoot"])  # type: ignore[arg-type]
    )
    entrypoint = next(entry for entry in files if entry["path"] == relative.as_posix())
    return {
        "schema_version": 1,
        "spec_sha256": CODEX_RUNTIME_SPEC_SHA256,
        "files": len(files),
        "entrypoint_sha256": entrypoint["sha256"],
    }


def _runtime_root(root: str | os.PathLike[str]) -> Path:
    destination = Path(root)
    if not destination.is_absolute():
        raise ValueError("Codex runtime root must be absolute")
    try:
        if not stat.S_ISDIR(destination.lstat().st_mode):
            raise ValueError("Codex runtime root must be a non-symlink directory")
    except OSError as error:
        raise ValueError("Codex runtime root is unavailable") from error
    return destination


def _scan_tree(destination: Path) -> tuple[set[str], set[str]]:
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for directory, names, filenames in os.walk(destination, followlinks=False):
        base = Path(directory)
        for name in names + filenames:
            path = base / name
            relative = path.relative_to(destination).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                actual_directories.add(relative)
            elif stat.S_ISREG(mode):
                actual_files.add(relative)
            else:
                raise ValueError(f"Codex runtime has a non-regular entry: {relative}")
    return actual_files, actual_directories


def _verify_tree_files(
    destination: Path, expected_files: dict[object, dict[str, object]]
) -> None:
    for relative, entry in expected_files.items():
        path = destination / relative  # type: ignore[operator]
        metadata = path.lstat()
        mode = int(entry["mode"], 8)  # type: ignore[arg-type]
        if stat.S_IMODE(metadata.st_mode) != mode or metadata.st_nlink != 1:
            raise ValueError(f"Codex runtime mode drifted: {relative}")
        _verify_regular_file(
            path,
            entry["bytes"],  # type: ignore[arg-type]
            entry["sha256"].removeprefix("sha256:"),  # type: ignore[union-attr]
        )


def verify_tree(root: str | os.PathLike[str], spec: object) -> dict[str, object]:
    """Verify the prepared runtime has exactly the pinned directories and files."""
    validated = validate_codex_runtime_spec(spec)
    destination = _runtime_root(root)
    files = validated["files"]
    assert isinstance(files, list)
    expected_files = {entry["path"]: entry for entry in files}
    actual_files, actual_directories = _scan_tree(destination)
    if actual_files != set(
        expected_files
    ) or actual_directories != _expected_directories(files):
        raise ValueError("Codex runtime tree shape drifted")
    _verify_tree_files(destination, expected_files)
    return _tree_receipt(validated)


def _write_member(
    bundle: tarfile.TarFile,
    member: tarfile.TarInfo,
    target: Path,
    entry: dict[str, object],
) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = bundle.extractfile(member)
    if source is None:
        raise ValueError("Codex archive member cannot be read")
    mode = int(entry["mode"], 8)  # type: ignore[arg-type]
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, mode)
    digest = hashlib.sha256()
    size = 0
    with source, os.fdopen(descriptor, "wb") as output:
        while chunk := source.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        os.fchmod(output.fileno(), mode)
    if size != entry["bytes"] or digest.hexdigest() != entry[  # type: ignore[union-attr]
        "sha256"
    ].removeprefix("sha256:"):
        raise ValueError("Codex archive member bytes drifted")


def _write_tree(
    bundle: tarfile.TarFile,
    expected: dict[str, dict[str, object]],
    root: Path,
) -> None:
    members: set[str] = set()
    for member in bundle:
        if member.name in members:
            raise ValueError("Codex archive contains a duplicate member")
        entry = expected.get(member.name)
        if (
            entry is None
            or not member.isfile()
            or member.size != entry["bytes"]
            or member.mode != int(entry["mode"], 8)  # type: ignore[arg-type]
        ):
            raise ValueError("Codex archive member set or metadata drifted")
        members.add(member.name)
        _write_member(bundle, member, root / entry["path"], entry)  # type: ignore[operator]
    if members != set(expected):
        raise ValueError("Codex archive member set or metadata drifted")


def prepare_tree(
    archive_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    spec: object,
) -> dict[str, object]:
    """Safely stream the exact archive files into a new absolute directory."""
    validated = validate_codex_runtime_spec(spec)
    root = Path(destination)
    if not root.is_absolute():
        raise ValueError("Codex runtime destination must be absolute")
    if root.exists() or root.is_symlink():
        raise ValueError("Codex runtime destination already exists")
    if not root.parent.is_dir():
        raise ValueError("Codex runtime destination parent is unavailable")

    archive = validated["archive"]
    files = validated["files"]
    assert isinstance(archive, dict) and isinstance(files, list)
    expected = {f"{archive['root']}/{entry['path']}": entry for entry in files}
    root.mkdir(mode=0o700)
    try:
        with (
            _verified_regular_file(
                Path(archive_path),
                archive["bytes"],  # type: ignore[arg-type]
                archive["sha256"].removeprefix("sha256:"),  # type: ignore[union-attr]
            ) as stream,
            tarfile.open(fileobj=stream, mode="r|gz") as bundle,
        ):
            _write_tree(bundle, expected, root)
        for relative in _expected_directories(files):
            (root / relative).chmod(0o755)
        root.chmod(0o755)
        return verify_tree(root, spec)
    except BaseException:
        shutil.rmtree(root)
        raise


def _tree_verification(root: str) -> str:
    directories = sorted(
        {
            parent.as_posix()
            for path, *_ in _FILES
            for parent in PurePosixPath(path).parents
            if parent.as_posix() != "."
        }
    )
    checks = [
        f"test -d {root}",
        f'test -z "$(find {root} -type l -print -quit)"',
        f'test -z "$(find {root} ! -type d ! -type f -print -quit)"',
        f'test "$(find {root} -type f | wc -l)" -eq {len(_FILES)}',
        f'test "$(find {root} -type d | wc -l)" -eq {len(directories) + 1}',
    ]
    checks.extend(f"test -d {root}/{path}" for path in directories)
    for path, mode, size, digest in _FILES:
        target = f"{root}/{path}"
        checks.extend(
            (
                f"test -f {target}",
                f'test "$(stat -c %a {target})" = {mode.lstrip("0")}',
                f'test "$(stat -c %s {target})" -eq {size}',
                f'test "$(stat -c %h {target})" -eq 1',
                f"printf '%s  %s\\n' {digest} {target} | sha256sum -c - >/dev/null",
            )
        )
    return "; ".join(checks)


def build_full_tree_verification_command(spec: object) -> str:
    """Build an exact-file, mode, size, hash, and tree-shape verification."""
    validate_codex_runtime_spec(spec)
    return "set -euo pipefail; " + _tree_verification(CODEX_RUNTIME_INSTALL_ROOT)


def rewrite_harbor_launch(command: str) -> str:
    """Replace Harbor 0.22's exact ambient Codex prefix with frozen bytes."""
    if type(command) is not str or not command.startswith(HARBOR_CODEX_EXEC_PREFIX):
        raise ValueError("unexpected Harbor Codex launch prefix")
    remainder = command[len(HARBOR_CODEX_EXEC_PREFIX) :]
    if not remainder or remainder[0].isspace():
        raise ValueError("unexpected Harbor Codex launch suffix")
    return f"{CODEX_RUNTIME_ENTRYPOINT} exec {remainder}"
