"""Safe handling of uploaded zip archives and of the zip we send back."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

MAX_RATIO = 200  # refuse archives that expand more than this many times


class ArchiveError(ValueError):
    """The uploaded archive is malformed or unsafe to unpack."""


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def safe_extract(archive: Path, dest: Path, *, max_bytes: int, max_files: int) -> list[Path]:
    """Unpack ``archive`` into ``dest``, refusing anything that escapes it.

    Guards against path traversal (``../``), absolute paths, symlinks and zip
    bombs. Returns the list of extracted files.
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0

    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a readable zip file: {exc}") from None

    with zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > max_files:
            raise ArchiveError(f"archive holds more than {max_files} files")

        compressed = sum(m.compress_size for m in members) or 1
        declared = sum(m.file_size for m in members)
        if declared > max_bytes or declared / compressed > MAX_RATIO:
            raise ArchiveError("archive expands to an implausible size")

        for member in members:
            if _is_symlink(member):
                continue
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue

            target = (dest / name).resolve()
            if not target.is_relative_to(dest):
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                while chunk := src.read(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArchiveError("archive expands past the configured limit")
                    out.write(chunk)
            extracted.append(target)

    if not extracted:
        raise ArchiveError("archive contained no usable files")
    return extracted


def build_zip(files: list[tuple[str, Path]], target: Path) -> Path:
    """Write ``(arcname, path)`` pairs into a deflated zip at ``target``."""
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in files:
            zf.write(path, arcname)
    return target
