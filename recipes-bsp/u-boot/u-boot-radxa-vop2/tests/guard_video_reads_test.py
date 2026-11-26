#!/usr/bin/env python3
"""Compile and exercise the extlinux video-read guard from the patch.

Two layers run here:

1. Helper tests compile the exact added bodies of
   arcadia_video_read_check() and arcadia_video_read_guarded() (extracted
   from 14-guard-extlinux-reads.patch) against fake gd/udevice objects.
2. Call-site test copies the real boot/bootmeth-uclass.c, applies the patch to
   that copy, extracts the patched bootmeth_common_read_file() and compiles it
   with stubs. It asserts fs_read() is never reached when the extlinux guard
   rejects the destination. This layer is skipped, with a message, when the
   U-Boot source tree is unavailable (set ARCADIA_UBOOT_SOURCE to override the
   default /tmp/arcadia-uboot-vop2/source).

Neither layer tests libfdt, a real bootstd walk, booti/FIT/EFI or other load
paths, memory outside the framebuffer range, hardware or IOMMU behaviour.
It needs a host C compiler.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

RECIPE = Path(__file__).resolve().parents[1]
PATCH = RECIPE / "14-guard-extlinux-reads.patch"
SOURCE = Path(os.environ.get("ARCADIA_UBOOT_SOURCE", "/tmp/arcadia-uboot-vop2/source"))


def patch_files():
    """Return the set of files the patch claims to modify."""
    return {line.split()[1][2:] for line in PATCH.read_text().splitlines()
            if line.startswith("--- a/")}


def added_function(name):
    """Return a whole added function body, requiring a contiguous addition."""
    lines = PATCH.read_text().splitlines()
    prefixes = ("+static int " + name + "(", "+static bool " + name + "(")
    start = next((i for i, line in enumerate(lines)
                  if line.startswith(prefixes)), None)
    if start is None:
        raise AssertionError(f"{name}: not found as an added static function")
    body = []
    for line in lines[start:]:
        if not line.startswith("+") or line.startswith("+++"):
            raise AssertionError(f"{name}: helper is not a contiguous addition")
        body.append(line[1:])
        if line == "+}":
            return "\n".join(body)
    raise AssertionError(f"{name}: unterminated")


def check_scope():
    """The patch must only touch boot/bootmeth-uclass.c, and add no includes."""
    files = patch_files()
    if files != {"boot/bootmeth-uclass.c"}:
        raise AssertionError(f"patch scope is {sorted(files)}")
    if re.search(r"^\+\s*#\s*include", PATCH.read_text(), re.MULTILINE):
        raise AssertionError("patch adds an include; only helper bodies are expected")


PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

typedef unsigned long ulong;

struct driver { const char *name; };
struct udevice { const char *name; struct driver *driver; };

struct fake_gd { ulong video_bottom, video_top; };
static struct fake_gd fake_gd;

#define gd (&fake_gd)
#define gd_video_bottom() (gd->video_bottom)
#define gd_video_top() (gd->video_top)
'''

HELPER_TESTS = r'''
#define FB_BOTTOM 0x7e000000UL
#define FB_TOP    0x80000000UL

static void video(ulong bottom, ulong top)
{
	fake_gd.video_bottom = bottom;
	fake_gd.video_top = top;
}

static int check(ulong addr, ulong size)
{
	return arcadia_video_read_check(addr, size);
}

int main(void)
{
	struct driver extlinux = { "bootmeth_extlinux" };
	struct driver script = { "bootmeth_2script" };
	struct udevice dev = { NULL, NULL };

	/* No reservation: the guard must not change any read. */
	video(0, 0);
	assert(check(0, 0) == 0);
	assert(check(0x1000, 0x1000) == 0);
	assert(check(0, ULONG_MAX) == 0);
	assert(check(ULONG_MAX, 1) == 0);

	/* Malformed: only one bound is set, so the range is unusable. */
	video(0, FB_TOP);
	assert(check(0x1000, 0x10) == -ERANGE);
	assert(check(0, 0) == -ERANGE);
	video(FB_BOTTOM, 0);
	assert(check(0x1000, 0x10) == -ERANGE);

	/* Malformed: inverted or empty range. */
	video(FB_TOP, FB_BOTTOM);
	assert(check(0x1000, 0x10) == -ERANGE);
	assert(check(FB_BOTTOM, 0) == -ERANGE);
	video(FB_BOTTOM, FB_BOTTOM);
	assert(check(0x1000, 0x10) == -ERANGE);

	video(FB_BOTTOM, FB_TOP);

	/* Zero length writes nothing, wherever it points. */
	assert(check(0, 0) == 0);
	assert(check(FB_BOTTOM, 0) == 0);
	assert(check(FB_BOTTOM + 0x100, 0) == 0);
	assert(check(FB_TOP - 1, 0) == 0);
	assert(check(ULONG_MAX, 0) == 0);

	/* Adjacent: the read ends exactly at bottom or starts exactly at top. */
	assert(check(0, FB_BOTTOM) == 0);
	assert(check(FB_BOTTOM - 0x1000, 0x1000) == 0);
	assert(check(FB_TOP, 0x1000) == 0);
	assert(check(FB_TOP, ULONG_MAX - FB_TOP) == 0);

	/* One byte of overlap on each side, then whole-range cases. */
	assert(check(FB_BOTTOM - 0x1000, 0x1001) == -EPERM);
	assert(check(FB_TOP - 1, 1) == -EPERM);
	assert(check(FB_BOTTOM, 1) == -EPERM);
	assert(check(FB_BOTTOM, FB_TOP - FB_BOTTOM) == -EPERM);
	assert(check(FB_BOTTOM + 0x10, 0x20) == -EPERM);
	assert(check(FB_BOTTOM - 0x10, 0x30) == -EPERM);
	assert(check(FB_TOP - 0x10, 0x30) == -EPERM);
	assert(check(FB_BOTTOM - 0x10, FB_TOP - FB_BOTTOM + 0x20) == -EPERM);
	assert(check(0, ULONG_MAX) == -EPERM);

	/* Wrapping end address is rejected before any range comparison. */
	assert(check(ULONG_MAX - 1, 4) == -ERANGE);
	assert(check(ULONG_MAX, 1) == -ERANGE);
	assert(check(FB_BOTTOM, ULONG_MAX - FB_BOTTOM + 2) == -ERANGE);
	assert(check(0, ULONG_MAX) != -ERANGE);

	/* Only the extlinux bootmeth is in scope. */
	assert(!arcadia_video_read_guarded(NULL));
	assert(!arcadia_video_read_guarded(&dev));
	dev.name = "1extlinux";
	assert(!arcadia_video_read_guarded(&dev));
	dev.name = "2script";
	assert(!arcadia_video_read_guarded(&dev));
	dev.driver = &script;
	dev.name = "script";
	assert(!arcadia_video_read_guarded(&dev));
	dev.driver = &extlinux;
	assert(arcadia_video_read_guarded(&dev));
	dev.name = NULL;
	assert(arcadia_video_read_guarded(&dev));
	dev.driver = NULL;
	assert(!arcadia_video_read_guarded(&dev));

	puts("video-read helper tests passed");
	return 0;
}
'''

CALLSITE_PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef unsigned long ulong;
typedef long long loff_t;
typedef unsigned int uint;

struct driver { const char *name; };
struct udevice { const char *name; struct driver *driver; };
struct blk_desc { int unused; };
struct bootflow_img { int unused; };

enum bootflow_img_t { BFI_SCRIPT = 0, BFI_IMAGE = 1 };

struct bootflow {
	const char *fname;
	struct udevice *blk;
	int part;
};

struct fake_gd { ulong video_bottom, video_top; };
static struct fake_gd fake_gd;

#define gd (&fake_gd)
#define gd_video_bottom() (gd->video_bottom)
#define gd_video_top() (gd->video_top)
#define log_msg_ret(name, ret) (ret)

/* Observation points for the filesystem read path. */
static int fs_read_calls;
static ulong fs_read_addr;
static loff_t fs_read_requested;
static loff_t fs_read_actual = -1;
static loff_t fs_size_value = 0x1000;
static int fs_size_ret;
static int fs_read_ret;
static int img_add_calls;

static void *dev_get_uclass_plat(const struct udevice *dev)
{
	(void)dev;
	return NULL;
}

static int bootmeth_setup_fs(struct bootflow *bflow, struct blk_desc *desc)
{
	(void)bflow;
	(void)desc;
	return 0;
}

static int fs_size(const char *path, loff_t *size)
{
	(void)path;
	*size = fs_size_value;
	return fs_size_ret;
}

static int fs_read(const char *path, ulong addr, loff_t offset, loff_t len,
		   loff_t *actread)
{
	(void)path;
	(void)offset;
	fs_read_calls++;
	fs_read_addr = addr;
	fs_read_requested = len;
	if (actread) {
		*actread = fs_read_actual >= 0 ? fs_read_actual : fs_size_value;
		if (len && *actread > len)
			*actread = len;
	}
	return fs_read_ret;
}

static struct bootflow_img *bootflow_img_add(struct bootflow *bflow,
					     const char *fname,
					     enum bootflow_img_t type,
					     ulong addr, uint size)
{
	(void)bflow;
	(void)fname;
	(void)type;
	(void)addr;
	(void)size;
	img_add_calls++;
	return (struct bootflow_img *)1;
}
'''

CALLSITE_TESTS = r'''
#define FB_BOTTOM 0x7e000000UL
#define FB_TOP    0x80000000UL

static struct udevice extlinux_dev = { "1extlinux", NULL };
static struct udevice script_dev = { "2script", NULL };
static struct driver extlinux_drv = { "bootmeth_extlinux" };
static struct driver script_drv = { "bootmeth_2script" };

static void reset(ulong bottom, ulong top, loff_t size)
{
	fake_gd.video_bottom = bottom;
	fake_gd.video_top = top;
	fs_size_value = size;
	fs_size_ret = 0;
	fs_read_ret = 0;
	fs_read_calls = 0;
	fs_read_addr = 0;
	fs_read_requested = -1;
	fs_read_actual = -1;
	img_add_calls = 0;
}

int main(void)
{
	struct bootflow bflow = { "extlinux.conf", NULL, 1 };
	ulong sizep;

	extlinux_dev.driver = &extlinux_drv;
	script_dev.driver = &script_drv;

	/* Extlinux read wholly inside the reservation: never writes. */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM + 0x1000, BFI_IMAGE,
					 &sizep) == -EPERM);
	assert(fs_read_calls == 0 && img_add_calls == 0);

	/* Extlinux read that crosses the bottom edge: rejected. */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM - 0x800, BFI_IMAGE,
					 &sizep) == -EPERM);
	assert(fs_read_calls == 0);

	/* Adjacent read below the reservation still reaches fs_read(). */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM - 0x1000, BFI_IMAGE,
					 &sizep) == 0);
	assert(fs_read_calls == 1 && fs_read_addr == FB_BOTTOM - 0x1000);
	assert(fs_read_requested == 0x1000);
	assert(sizep == 0x1000 && img_add_calls == 1);

	/* Adjacent read above the reservation still reaches fs_read(). */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_TOP, BFI_IMAGE, &sizep) == 0);
	assert(fs_read_calls == 1);

	/* Wrapping destination: rejected before any write. */
	reset(FB_BOTTOM, FB_TOP, 4);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 ULONG_MAX - 1, BFI_IMAGE,
					 &sizep) == -ERANGE);
	assert(fs_read_calls == 0);

	/* Malformed video range: fail closed rather than guess. */
	reset(0, FB_TOP, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 0x1000, BFI_IMAGE, &sizep) == -ERANGE);
	assert(fs_read_calls == 0);
	reset(FB_TOP, FB_BOTTOM, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 0x1000, BFI_IMAGE, &sizep) == -ERANGE);
	assert(fs_read_calls == 0);

	/* No video reservation at all: reads behave exactly as before. */
	reset(0, 0, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM + 0x1000, BFI_IMAGE,
					 &sizep) == 0);
	assert(fs_read_calls == 1);

	/* Not extlinux: the guard must not change that bootmeth at all. */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&script_dev, &bflow, "Image",
					 FB_BOTTOM + 0x1000, BFI_IMAGE,
					 &sizep) == 0);
	assert(fs_read_calls == 1 && fs_read_requested == 0);

	/* len=0 means read the whole file, so a zero stat must fail closed. */
	reset(FB_BOTTOM, FB_TOP, 0);
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM, BFI_IMAGE, &sizep) == -EINVAL);
	assert(fs_read_calls == 0 && img_add_calls == 0);

	/* If the backing file grows, fs_read is still capped at the checked size. */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	fs_read_actual = 0x1800;
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM - 0x1000, BFI_IMAGE, &sizep) == 0);
	assert(fs_read_requested == 0x1000 && sizep == 0x1000);

	/* A short read is not a successfully loaded boot payload. */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	fs_read_actual = 0x800;
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM - 0x1000, BFI_IMAGE, &sizep) == -EIO);
	assert(fs_read_requested == 0x1000 && img_add_calls == 0);

	/* Capacity check stays first: no read, and not the guard's error. */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	sizep = 0x10;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 FB_BOTTOM, BFI_IMAGE,
					 &sizep) == -ENOSPC);
	assert(fs_read_calls == 0);

	/* fs_read() failure is still propagated unchanged. */
	reset(FB_BOTTOM, FB_TOP, 0x1000);
	fs_read_ret = -EIO;
	sizep = 1 << 30;
	assert(bootmeth_common_read_file(&extlinux_dev, &bflow, "Image",
					 0x1000, BFI_IMAGE, &sizep) == -EIO);
	assert(fs_read_calls == 1);

	puts("video-read call-site tests passed");
	return 0;
}
'''


def compile_and_run(tmp, name, source):
    src = Path(tmp) / f"{name}.c"
    exe = Path(tmp) / name
    src.write_text(source)
    # -Wno-sign-compare: the unmodified `size > *sizep` line in the real
    # bootmeth_common_read_file() predates this diagnostic and is not ours.
    subprocess.run(["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra",
                    "-Werror", "-Wno-unused-parameter", "-Wno-sign-compare",
                    str(src), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def patched_read_file():
    """Apply the patch to a copy of the real source and return the function."""
    extlinux = (SOURCE / "boot" / "bootmeth_extlinux.c").read_text()
    if not re.search(r'U_BOOT_DRIVER\(bootmeth_1extlinux\)\s*=\s*\{\s*\.name\s*=\s*"bootmeth_extlinux"', extlinux):
        raise AssertionError("extlinux registered driver name changed; guard needs review")
    with tempfile.TemporaryDirectory(prefix="arcadia-reads-src-") as tmp:
        work = Path(tmp)
        (work / "boot").mkdir()
        shutil.copy(SOURCE / "boot" / "bootmeth-uclass.c",
                    work / "boot" / "bootmeth-uclass.c")
        proc = subprocess.run(["patch", "-p1", "-s", "-i", str(PATCH)], cwd=work,
                              capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError(f"patch did not apply: {proc.stdout}{proc.stderr}")
        text = (work / "boot" / "bootmeth-uclass.c").read_text()
    start = text.index("int bootmeth_common_read_file(struct udevice *dev,")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unterminated bootmeth_common_read_file")


def main():
    check_scope()
    helpers = "\n\n".join(added_function(name) for name in (
        "arcadia_video_read_check", "arcadia_video_read_guarded"))
    with tempfile.TemporaryDirectory(prefix="arcadia-reads-test-") as tmp:
        compile_and_run(tmp, "helper_test", PRELUDE + helpers + HELPER_TESTS)
        if not all((SOURCE / "boot" / name).is_file()
                   for name in ("bootmeth-uclass.c", "bootmeth_extlinux.c")):
            if "ARCADIA_UBOOT_SOURCE" in os.environ:
                raise SystemExit(f"call-site tests: incomplete U-Boot source at {SOURCE}")
            print(f"call-site tests skipped: no U-Boot source at {SOURCE}")
            return
        function = patched_read_file()
        compile_and_run(tmp, "callsite_test",
                        CALLSITE_PRELUDE + helpers + "\n\n" + function +
                        CALLSITE_TESTS)


if __name__ == "__main__":
    main()
