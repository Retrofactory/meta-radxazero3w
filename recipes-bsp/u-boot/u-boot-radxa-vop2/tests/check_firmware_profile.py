#!/usr/bin/env python3
"""Report whether a built U-Boot artifact's markers match an opt-in profile.

Marker presence is link-level evidence only: it does not show that the code
runs, that a guard is reachable at boot, or that handoff is safe. Exit codes:
0 markers match the profile, 1 mismatch, 2 bad input/usage.
"""
from pathlib import Path
import sys

# arcadia-boot-artwork.patch: expected in both profiles.
ARTWORK = b"Arcadia artwork: %s"

# One full printf literal per guard. Each is unique to its patch, so never
# search the shorter "relocate %lx size %lx, video %lx-%lx" tail alone.
MARKERS = {
    "extlinux guard": b"Arcadia video guard: %s at %lx size %lx, video %lx-%lx",
    "booti guard": b"Arcadia booti guard: kernel_comp_size %lx cannot be scaled",
    "bootm guard": b"Arcadia bootm guard: decompress %lx bound %lx, "
                   b"video %lx-%lx err %d",
    "handoff": b"Arcadia handoff: reserved framebuffer %lx+%lx for 1080p60 HDMI",
}

# guarded -> all four present; cleanup -> all four absent.
WANTED = {"cleanup": False, "guarded": True}


def read_bytes(path, kind):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        print(f"error: cannot read {kind} {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not data:
        print(f"error: {kind} is empty: {path}", file=sys.stderr)
        raise SystemExit(2)
    return data


def config_values(text):
    return dict(line.strip().split("=", 1) for line in text.splitlines()
                if line.strip().startswith("CONFIG_") and "=" in line)


def main(argv):
    if len(argv) != 4 or argv[1] not in WANTED:
        print("usage: check_firmware_profile.py {cleanup|guarded} "
              "<u-boot.bin> <.config>", file=sys.stderr)
        return 2
    profile, firmware, dotconfig = argv[1:]
    want = WANTED[profile]
    blob = read_bytes(firmware, "artifact")
    cfg = config_values(read_bytes(dotconfig, ".config").decode("utf-8", "replace"))

    fails = []
    if cfg.get("CONFIG_VIDEO_REMOVE") != "y":
        fails.append("CONFIG_VIDEO_REMOVE=y not set")
    if ARTWORK not in blob:
        fails.append(f"shared artwork literal absent: {ARTWORK.decode()}")
    for name, literal in MARKERS.items():
        found = literal in blob
        print(f"  {name:<15} {'present' if found else 'absent'}")
        if found != want:
            expect = "present" if want else "absent"
            fails.append(f"{name} literal {literal.decode()!r} is "
                         f"{'present' if found else 'absent'}, expected {expect}")
    if want and cfg.get("CONFIG_OF_BOARD_SETUP") != "y":
        fails.append("guarded profile needs CONFIG_OF_BOARD_SETUP=y")

    print(f"\nprofile: {profile} -> {'MATCH' if not fails else 'MISMATCH'}")
    for line in fails:
        print(f"  FAIL: {line}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
