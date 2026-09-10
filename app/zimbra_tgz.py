"""EML -> Zimbra mail-only TGZ. Basic import confirmed by user on ZCS 8.8.15.

Uses ArchiveFormatter.addData (no .meta): entry path is the folder, mtime is
the received timestamp, and mode without owner-write represents unread mail.
Original MIME bytes are copied verbatim. Other IMAP flags are not represented.
"""
import argparse
import json
import tarfile
from pathlib import Path


def _relative(value):
    if not isinstance(value, str) or not value:
        raise ValueError("A nonempty relative path is required")
    parts = value.split("/")
    if any(p in ("", ".", "..") or any(c in p for c in '\\:*?"<>|')
           or any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError("Unsafe or unsupported path: " + repr(value))
    return parts


def build_tgz(root, records, destination):
    """records: file, folder, received_at (Unix seconds), seen (boolean).

    A separate manifest retains flags and source identifiers for future import
    implementations. Do not put that manifest inside this TGZ: Zimbra would
    treat an arbitrary JSON file as a Briefcase document.
    """
    root = Path(root).resolve(strict=True)
    destination = Path(destination)
    count = 0
    # Exclusive creation deliberately refuses to replace existing artifacts.
    stream = destination.open("xb")
    try:
        with stream, tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.GNU_FORMAT,
                                  encoding="utf-8") as archive:
            for count, row in enumerate(records, 1):
                source = root.joinpath(*_relative(row["file"])).resolve(strict=True)
                if not source.is_relative_to(root) or not source.is_file():
                    raise ValueError("Source escapes mailbox directory or is not a file")
                folder = "/".join(_relative(row["folder"]))
                stamp, seen = row["received_at"], row["seen"]
                if type(stamp) is not int or not 0 <= stamp <= 2147483647:
                    raise ValueError("received_at must be Unix seconds in supported range")
                if type(seen) is not bool:
                    raise ValueError("seen must be a boolean")
                item = tarfile.TarInfo(f"{folder}/{count:010d}.eml")
                item.size = source.stat().st_size
                item.mtime = stamp
                item.mode = 0o600 if seen else 0o400
                with source.open("rb") as message:
                    archive.addfile(item, message)
            if not count:
                raise ValueError("No messages supplied")
    except BaseException:
        stream.close()
        destination.unlink(missing_ok=True)
        raise
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    print(f"Packed {build_tgz(args.manifest.parent, rows, args.output)} messages")


if __name__ == "__main__":
    main()
