#!/usr/bin/env python3
"""Compile and exercise the bootm framebuffer pre-write guards from the WIP patch.

Three layers run here:

1. Patch-scope checks: 16-bootm-write-guards.WIP.patch must touch only
   boot/bootm.c, may add only <limits.h>, and must contain no removed line, so
   every existing load, blob-overlap and booti setup step in that file is
   unchanged.
2. Helper tests compile the exact added bodies of
   arcadia_bootm_video_write_check() and arcadia_bootm_decomp_guard()
   (extracted from the patch) against a fake global_data, and check that the
   same bodies are what the patch actually installs into boot/bootm.c. They
   cover the per-compression-type output bound, the uncompressed exact length,
   the no-copy case, an output bound that cannot be expressed and a
   destination whose end address would wrap.
3. Call-site tests copy the real boot/bootm.c, apply the patch to that copy,
   extract the patched bootm_load_os() and compile it with stubs for
   image_decomp(), booti_setup(), handle_decomp_error(), flush_cache(),
   memmove(), LMB and the bootstage hooks. They assert that no decompression
   write and no relocation write happens when the guard rejects the
   destination, and that a permitted load still reaches the same stubs in the
   same order. This layer is skipped, with a message, when the U-Boot source
   tree is unavailable (set ARCADIA_UBOOT_SOURCE to override the default
   /tmp/arcadia-uboot-vop2/source).

None of this tests a real decompressor, an actual bootm/bootstd run, the
contents of the video reservation, LMB bookkeeping, or hardware. The output
bound asserted for each compression type is a reading of the in-tree
decompressor sources, not something these tests execute. Not covered by the
patch at all: FIT/bootm payload pre-loading, raw load/acp commands, initrd and
FDT placement, EFI loaders, other bootmeths, cache maintenance and an active
IOMMU during handoff. It needs a host C compiler.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

RECIPE = Path(__file__).resolve().parents[1]
PATCH = RECIPE / "16-bootm-write-guards.WIP.patch"
SOURCE = Path(os.environ.get("ARCADIA_UBOOT_SOURCE", "/tmp/arcadia-uboot-vop2/source"))


def patch_lines():
    lines = PATCH.read_text().splitlines()
    # Everything before the bare --- separator is the commit message.
    return lines, lines[lines.index("---") + 1:]


def check_scope():
    """The patch must only touch boot/bootm.c and only add lines."""
    lines, body = patch_lines()
    files = {line.split()[1][2:] for line in lines if line.startswith("--- a/")}
    if files != {"boot/bootm.c"}:
        raise AssertionError(f"patch scope is {sorted(files)}")
    includes = [line for line in body if re.match(r"\+\s*#\s*include", line)]
    if includes != ["+#include <limits.h>"]:
        raise AssertionError(f"unexpected added includes: {includes}")
    removed = [line for line in body
               if line.startswith("-") and not line.startswith("--- a/")]
    if removed:
        raise AssertionError(f"patch removes existing lines: {removed}")
    if body[0] != "--- a/boot/bootm.c":
        raise AssertionError(f"unexpected first hunk header {body[0]!r}")
    added = [line for line in body if line.startswith("+static int ")]
    if added != ["+static int arcadia_bootm_video_write_check(ulong addr, ulong size)",
                 "+static int arcadia_bootm_decomp_guard(int comp, ulong load, ulong image_start,"]:
        raise AssertionError(f"unexpected added functions: {added}")


def added_function(name):
    """Return a whole added function body, requiring a contiguous addition."""
    lines, _ = patch_lines()
    prefixes = ("+static int " + name + "(",
                "+static void " + name + "(",
                "+static bool " + name + "(")
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


BASE_PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

typedef unsigned long ulong;
typedef unsigned int uint;

/* include/linux/errno.h:160 in the U-Boot tree; the host libc has none. */
#ifndef ENOTSUPP
#define ENOTSUPP 524
#endif

/* include/image.h:249 (compression types are appended to, never renumbered). */
enum {
	IH_COMP_NONE = 0,
	IH_COMP_GZIP,
	IH_COMP_BZIP2,
	IH_COMP_LZMA,
	IH_COMP_LZO,
	IH_COMP_LZ4,
	IH_COMP_ZSTD,
};

struct fake_gd { ulong video_bottom, video_top; };
static struct fake_gd fake_gd;

#define gd (&fake_gd)
#define gd_video_bottom() (gd->video_bottom)
#define gd_video_top() (gd->video_top)
'''

HELPER_TESTS = r'''
#define FB_BOTTOM 0x7e000000UL
#define FB_TOP    0x80000000UL
/* The Radxa Zero 3W builds with the ARM64 Kconfig default. */
#define UNC_LEN   0x8000000UL

static void video(ulong bottom, ulong top)
{
	fake_gd.video_bottom = bottom;
	fake_gd.video_top = top;
}

static int guard(int comp, ulong load, ulong image_start, ulong image_len,
		 ulong unc_len)
{
	return arcadia_bootm_decomp_guard(comp, load, image_start, image_len,
					  unc_len);
}

int main(void)
{
	/* No recorded reservation (also the built-in case for !CONFIG_VIDEO). */
	video(0, 0);
	assert(guard(IH_COMP_NONE, 0x1000, 0x2000, 0x100, UNC_LEN) == 0);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x100, UNC_LEN) == 0);
	assert(guard(IH_COMP_ZSTD, 0x1000, 0x2000, 0x100, UNC_LEN) == 0);
	assert(arcadia_bootm_video_write_check(0, 0) == 0);
	assert(arcadia_bootm_video_write_check(0x1000, 0x1000) == 0);
	assert(arcadia_bootm_video_write_check(0, ULONG_MAX) == 0);
	assert(arcadia_bootm_video_write_check(ULONG_MAX, 1) == 0);
	assert(arcadia_bootm_video_write_check(FB_BOTTOM,
					       FB_TOP - FB_BOTTOM) == 0);

	video(FB_BOTTOM, FB_TOP);

	/*
	 * No-copy: an uncompressed image already at its load address is not
	 * written at all, so no window is checked and even a malformed range
	 * does not block the boot.
	 */
	assert(guard(IH_COMP_NONE, FB_BOTTOM, FB_BOTTOM, 0x1000, UNC_LEN) == 0);
	assert(guard(IH_COMP_NONE, FB_TOP - 1, FB_TOP - 1, 1, UNC_LEN) == 0);
	video(0, FB_TOP);
	assert(guard(IH_COMP_NONE, FB_BOTTOM, FB_BOTTOM, 0x1000, UNC_LEN) == 0);
	video(FB_TOP, FB_BOTTOM);
	assert(guard(IH_COMP_NONE, FB_BOTTOM, FB_BOTTOM, 0x1000, UNC_LEN) == 0);
	video(FB_BOTTOM, FB_BOTTOM);
	assert(guard(IH_COMP_NONE, 0x1000, 0x1000, 0x1000, UNC_LEN) == 0);
	video(FB_BOTTOM, FB_TOP);

	/* The no-copy shortcut must not leak into the compressed path. */
	assert(guard(IH_COMP_GZIP, FB_BOTTOM, FB_BOTTOM, 0x1000,
		     UNC_LEN) == -EPERM);
	assert(guard(IH_COMP_LZ4, FB_BOTTOM, FB_BOTTOM, 0x1000,
		     UNC_LEN) == -EPERM);

	/*
	 * Uncompressed copy: the window is exactly image_len bytes, not the
	 * buffer size.
	 */
	assert(guard(IH_COMP_NONE, FB_BOTTOM - 0x1000, 0x1000, 0x1000,
		     UNC_LEN) == 0);
	assert(guard(IH_COMP_NONE, FB_BOTTOM - 0x1000, 0x1000, 0x1001,
		     UNC_LEN) == -EPERM);
	assert(guard(IH_COMP_NONE, FB_BOTTOM - 0x800, 0x1000, 0x1000,
		     UNC_LEN) == -EPERM);
	assert(guard(IH_COMP_NONE, FB_TOP, 0x1000, 0x1000, UNC_LEN) == 0);
	assert(guard(IH_COMP_NONE, FB_TOP - 1, 0x1000, 1, UNC_LEN) == -EPERM);
	assert(guard(IH_COMP_NONE, FB_TOP - 1, 0x1000, 0x1000,
		     UNC_LEN) == -EPERM);
	/* A zero-length copy writes nothing, wherever it points. */
	assert(guard(IH_COMP_NONE, FB_BOTTOM, 0x1000, 0, UNC_LEN) == 0);

	/* An uncompressed image larger than the buffer is never copied. */
	assert(guard(IH_COMP_NONE, 0x1000, 0x2000, UNC_LEN + 1,
		     UNC_LEN) == -ENOSPC);
	assert(guard(IH_COMP_NONE, 0x1000, 0x2000, UNC_LEN, UNC_LEN) == 0);

	/*
	 * Compressed: the bound is the buffer size handed to image_decomp(),
	 * because the real output length is not known before decompressing.
	 */
	assert(guard(IH_COMP_GZIP, FB_BOTTOM - 0x100, 0x1000, 0x10,
		     UNC_LEN) == -EPERM);
	assert(guard(IH_COMP_GZIP, FB_BOTTOM, 0x1000, 0x10, 0x100) == -EPERM);
	assert(guard(IH_COMP_GZIP, FB_TOP, 0x1000, 0x10, 0x100) == 0);
	assert(guard(IH_COMP_BZIP2, FB_BOTTOM - 0x100, 0x1000, 0x10, 0x100) == 0);
	assert(guard(IH_COMP_BZIP2, FB_BOTTOM - 0x100, 0x1000, 0x10,
		     0x101) == -EPERM);
	assert(guard(IH_COMP_LZMA, FB_BOTTOM - 0x100, 0x1000, 0x10, 0x100) == 0);
	assert(guard(IH_COMP_LZMA, FB_BOTTOM - 0x100, 0x1000, 0x10,
		     0x101) == -EPERM);
	assert(guard(IH_COMP_LZO, FB_BOTTOM - 0x100, 0x1000, 0x10, 0x100) == 0);
	assert(guard(IH_COMP_LZO, FB_BOTTOM - 0x100, 0x1000, 0x10,
		     0x101) == -EPERM);
	assert(guard(IH_COMP_LZ4, FB_BOTTOM - 0x100, 0x1000, 0x10, 0x100) == 0);
	assert(guard(IH_COMP_LZ4, FB_BOTTOM - 0x100, 0x1000, 0x10,
		     0x101) == -EPERM);
	assert(guard(IH_COMP_ZSTD, FB_BOTTOM - 0x100, 0x1000, 0x10, 0x100) == 0);
	assert(guard(IH_COMP_ZSTD, FB_BOTTOM - 0x100, 0x1000, 0x10,
		     0x101) == -EPERM);
	/* A window starting at zero spans the whole reservation. */
	assert(guard(IH_COMP_ZSTD, 0, 0x1000, 0x10, FB_TOP) == -EPERM);
	/* ... and that same gzip bound is refused before the range is compared. */
	assert(guard(IH_COMP_GZIP, 0, 0x1000, 0x10, FB_TOP) == -ERANGE);

	/*
	 * A malformed recorded range fails closed only when a write would
	 * happen: a partial range, an inverted range and an empty range.
	 */
	video(0, FB_TOP);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10, UNC_LEN) == -ERANGE);
	assert(guard(IH_COMP_NONE, 0x1000, 0x2000, 0x10, UNC_LEN) == -ERANGE);
	assert(arcadia_bootm_video_write_check(0x1000, 0x10) == -ERANGE);
	assert(arcadia_bootm_video_write_check(0, 0) == -ERANGE);
	video(FB_BOTTOM, 0);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10, UNC_LEN) == -ERANGE);
	video(FB_TOP, FB_BOTTOM);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10, UNC_LEN) == -ERANGE);
	video(FB_BOTTOM, FB_BOTTOM);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10, UNC_LEN) == -ERANGE);
	video(FB_BOTTOM, FB_TOP);

	/*
	 * A window whose end address would wrap is refused, and a gzip bound
	 * that does not survive the int conversion lib/gunzip.c makes is
	 * refused rather than checked with a narrower window.
	 */
	assert(guard(IH_COMP_NONE, ULONG_MAX - 0x10, 0x1000, 0x1000,
		     ULONG_MAX) == -ERANGE);
	assert(guard(IH_COMP_GZIP, ULONG_MAX - 0x10, 0x1000, 0x10,
		     UNC_LEN) == -ERANGE);
	assert(guard(IH_COMP_ZSTD, ULONG_MAX - 0x10, 0x1000, 0x10,
		     UNC_LEN) == -ERANGE);
	video(0, 0);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10,
		     (ulong)INT_MAX) == 0);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10,
		     (ulong)INT_MAX + 1) == -ERANGE);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10,
		     0x80000000UL) == -ERANGE);
#if ULONG_MAX > UINT_MAX
	/* Truncation to a positive int must not bypass the range check. */
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10,
		     (ulong)UINT_MAX + 1) == -ERANGE);
	assert(guard(IH_COMP_GZIP, 0x1000, 0x2000, 0x10,
		     (ulong)UINT_MAX + 2) == -ERANGE);
#endif
	/* The other types take a size_t or uint length and keep the bound. */
	assert(guard(IH_COMP_ZSTD, 0x1000, 0x2000, 0x10,
		     (ulong)INT_MAX + 1) == 0);
	assert(guard(IH_COMP_LZO, 0x1000, 0x2000, 0x10,
		     (ulong)INT_MAX + 1) == 0);
	video(FB_BOTTOM, FB_TOP);

	/* A compression type this guard cannot bound is refused. */
	assert(guard(IH_COMP_ZSTD + 1, 0x1000, 0x2000, 0x10,
		     UNC_LEN) == -ENOTSUPP);
	assert(guard(99, 0x1000, 0x2000, 0x10, UNC_LEN) == -ENOTSUPP);
	video(0, 0);
	assert(guard(99, 0x1000, 0x2000, 0x10, UNC_LEN) == -ENOTSUPP);

	puts("bootm write helper tests passed");
	return 0;
}
'''

CALLSITE_PRELUDE = r'''
typedef unsigned int u32;
typedef unsigned long long u64;
typedef unsigned long long phys_addr_t;
typedef unsigned long long phys_size_t;

/* include/image.h values used by bootm_load_os(). */
enum { IH_TYPE_KERNEL = 2, IH_TYPE_MULTI = 4, IH_TYPE_KERNEL_NOLOAD = 14 };
enum { IH_OS_LINUX = 5 };
enum { IH_ARCH_ARM64 = 22 };

/* Kconfig: default 0x8000000 for ARM64. */
#define CONFIG_SYS_BOOTM_LEN 0x8000000
#define CONFIG_CMD_BOOTI 1
#define CONFIG_LMB 1

/* Both expand to the plain preprocessor flag, as the real macros do. */
#define IS_ENABLED(x) (x)
#define CONFIG_IS_ENABLED(x) (CONFIG_##x)

#define SZ_1M 0x00100000
#define SZ_2M 0x00200000
#define ARCH_DMA_MINALIGN 64
#define ALIGN(x, a) (((x) + (ulong)(a) - 1) & ~((ulong)(a) - 1))
#define ALIGN_DOWN(x, a) ((x) & ~((ulong)(a) - 1))

#define BOOTM_ERR_RESET (-1)
#define BOOTM_ERR_OVERLAP (-2)
#define BOOTM_ERR_UNIMPLEMENTED (-3)

enum {
	BOOTSTAGE_ID_DECOMP_IMAGE = 1,
	BOOTSTAGE_ID_KERNEL_LOADED,
	BOOTSTAGE_ID_OVERWRITTEN,
};

enum lmb_mem_type { LMB_MEM_ALLOC_ANY = 1, LMB_MEM_ALLOC_ADDR = 2 };
#define LMB_NONE 0

struct legacy_img_hdr { int type; };

struct image_info {
	ulong start, end, image_start, image_len, load;
	int type, comp, os, arch;
};

struct bootm_headers {
	struct image_info os;
	ulong ep;
	bool legacy_hdr_valid;
	struct legacy_img_hdr legacy_hdr_os_copy;
};

/* Knobs the test sets before calling bootm_load_os(). */
static ulong decomp_out_len;
static int decomp_ret;
static ulong reloc_addr, setup_image_size;
static int setup_ret;
static ulong lmb_alloc_any_addr;

/* Observations. */
static int decomp_calls, decomp_error_calls, setup_calls, lmb_calls;
static int lmb_any_calls, flush_calls, mark_calls, bootstage_error_calls;
static int printf_calls, puts_calls, debug_calls, log_err_calls, guard_msgs;
static ulong seen_decomp_load, seen_decomp_image_start, seen_decomp_image_len;
static ulong seen_decomp_unc_len, seen_decomp_type, seen_decomp_comp;
static ulong seen_lmb_any_size;
static int memmove_calls;
static ulong memmove_dst[4], memmove_len[4];

static void *map_sysmem(ulong paddr, unsigned long size)
{
	(void)size;
	return (void *)paddr;
}

static int image_decomp(int comp, ulong load, ulong image_start, int type,
			void *load_buf, void *image_buf, ulong image_len,
			uint unc_len, ulong *load_end)
{
	(void)load_buf;
	(void)image_buf;
	decomp_calls++;
	seen_decomp_comp = (ulong)comp;
	seen_decomp_load = load;
	seen_decomp_image_start = image_start;
	seen_decomp_image_len = image_len;
	seen_decomp_type = (ulong)type;
	seen_decomp_unc_len = unc_len;
	*load_end = load + decomp_out_len;
	return decomp_ret;
}

static int handle_decomp_error(int comp_type, size_t uncomp_size,
			       size_t buf_size, int ret)
{
	(void)comp_type;
	(void)uncomp_size;
	(void)buf_size;
	decomp_error_calls++;
	return ret == -ENOSYS ? BOOTM_ERR_UNIMPLEMENTED : 1;
}

static int booti_setup(ulong image, ulong *relocated, ulong *size,
		       bool force_reloc)
{
	(void)image;
	(void)force_reloc;
	setup_calls++;
	*relocated = reloc_addr;
	*size = setup_image_size;
	return setup_ret;
}

static int lmb_alloc_mem(enum lmb_mem_type type, u64 align, phys_addr_t *addr,
			 phys_size_t size, u32 flags)
{
	(void)align;
	(void)flags;
	if (type == LMB_MEM_ALLOC_ANY) {
		lmb_any_calls++;
		seen_lmb_any_size = (ulong)size;
		*addr = (phys_addr_t)lmb_alloc_any_addr;
		return 0;
	}
	lmb_calls++;
	return 0;
}

static void flush_cache(ulong start, size_t size)
{
	(void)start;
	(void)size;
	flush_calls++;
}

static void bootstage_mark(int id)
{
	(void)id;
	mark_calls++;
}

static void bootstage_error(int id)
{
	(void)id;
	bootstage_error_calls++;
}

static int image_get_type(const struct legacy_img_hdr *hdr)
{
	return hdr->type;
}

static void *arcadia_memmove(void *dst, const void *src, size_t n)
{
	(void)src;
	if (memmove_calls < 4) {
		memmove_dst[memmove_calls] = (ulong)dst;
		memmove_len[memmove_calls] = (ulong)n;
	}
	memmove_calls++;
	return dst;
}
/* The host headers may define memmove as a fortified macro; record ours. */
#undef memmove
#define memmove arcadia_memmove

static int quiet_printf(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	va_end(ap);
	printf_calls++;
	if (strstr(fmt, "Arcadia bootm guard"))
		guard_msgs++;
	return 0;
}
#define printf quiet_printf

static int quiet_puts(const char *str)
{
	(void)str;
	puts_calls++;
	return 0;
}
#define puts quiet_puts

static void quiet_debug(const char *fmt, ...)
{
	(void)fmt;
	debug_calls++;
}
#define debug(...) quiet_debug(__VA_ARGS__)

static void log_err(const char *fmt, ...)
{
	(void)fmt;
	log_err_calls++;
}
'''

STATIC_FUNC = "static int bootm_load_os(struct bootm_headers *images, int boot_progress)"

CALLSITE_TESTS = r'''
/* Drop the puts() redirection so the test can report success itself. */
#undef puts

#define FB_BOTTOM 0x7e000000UL
#define FB_TOP    0x80000000UL
#define LOAD_ADDR 0x40200000UL
#define SRC_ADDR  0x48000000UL
#define UNC_LEN   ((ulong)CONFIG_SYS_BOOTM_LEN)

static struct bootm_headers fake_images;

static void reset(void)
{
	fake_gd.video_bottom = 0;
	fake_gd.video_top = 0;
	fake_images.os.start = SRC_ADDR;
	fake_images.os.end = SRC_ADDR + 0x100000;
	fake_images.os.image_start = SRC_ADDR;
	fake_images.os.image_len = 0x100000;
	fake_images.os.load = LOAD_ADDR;
	fake_images.os.type = IH_TYPE_KERNEL;
	fake_images.os.comp = IH_COMP_NONE;
	fake_images.os.arch = IH_ARCH_ARM64;
	fake_images.os.os = IH_OS_LINUX;
	fake_images.ep = 0;
	fake_images.legacy_hdr_valid = false;
	fake_images.legacy_hdr_os_copy.type = IH_TYPE_KERNEL;
	decomp_out_len = 0x200000;
	decomp_ret = 0;
	reloc_addr = LOAD_ADDR;
	setup_image_size = 0x1000;
	setup_ret = 0;
	lmb_alloc_any_addr = 0x50000000;
	decomp_calls = decomp_error_calls = setup_calls = lmb_calls = 0;
	lmb_any_calls = flush_calls = mark_calls = bootstage_error_calls = 0;
	printf_calls = puts_calls = debug_calls = log_err_calls = guard_msgs = 0;
	seen_decomp_load = seen_decomp_image_start = seen_decomp_image_len = 0;
	seen_decomp_unc_len = seen_decomp_type = seen_decomp_comp = 0;
	seen_lmb_any_size = 0;
	memmove_calls = 0;
	memset(memmove_dst, 0, sizeof(memmove_dst));
	memset(memmove_len, 0, sizeof(memmove_len));
}

/* Move the load address without asking for a relocation. */
static void set_load(ulong addr)
{
	fake_images.os.load = addr;
	reloc_addr = addr;
}

static void video(ulong bottom, ulong top)
{
	fake_gd.video_bottom = bottom;
	fake_gd.video_top = top;
}

/*
 * A stub memmove must never have been handed a video-overlapping range. With a
 * malformed recorded range the guard fails closed, so no relocation write may
 * happen at all.
 */
static void assert_no_video_write(void)
{
	ulong bottom = fake_gd.video_bottom, top = fake_gd.video_top;
	int i;

	if (!bottom && !top)
		return;
	if (!bottom || top <= bottom) {
		assert(memmove_calls == 0);
		return;
	}
	for (i = 0; i < memmove_calls && i < 4; i++) {
		ulong dst = memmove_dst[i], n = memmove_len[i];

		assert(!n || dst >= top || dst + n <= bottom);
	}
}

/*
 * BOOTM_ERR_RESET is -1, the same value as -EPERM, so these tests use the
 * recorded call order - not the return value alone - to tell a guard refusal
 * from an ordinary decompression failure.
 */
static int load(void)
{
	int (*const load_os)(struct bootm_headers *, int) = bootm_load_os;
	int rc = load_os(&fake_images, 0);

	assert_no_video_write();
	return rc;
}

int main(void)
{
	int rc;

	/* Uncompressed image already in place: no write, no reservation. */
	reset();
	fake_images.os.load = SRC_ADDR;
	reloc_addr = SRC_ADDR;
	rc = load();
	assert(rc == 0 && guard_msgs == 0);
	assert(decomp_calls == 1 && memmove_calls == 0);
	assert(seen_decomp_comp == IH_COMP_NONE);
	assert(seen_decomp_load == SRC_ADDR);
	assert(seen_decomp_unc_len == UNC_LEN);
	assert(flush_calls == 1 && mark_calls == 1);
	assert(setup_calls == 1 && lmb_calls == 1);
	assert(fake_images.ep == reloc_addr);
	assert(fake_images.os.start == reloc_addr);
	assert(fake_images.os.end == reloc_addr + setup_image_size);
	assert(fake_images.os.image_len == decomp_out_len);

	/* The same boot with a malformed reservation has nothing to write. */
	reset();
	fake_images.os.load = SRC_ADDR;
	reloc_addr = SRC_ADDR;
	video(FB_TOP, FB_BOTTOM);
	rc = load();
	assert(rc == 0 && guard_msgs == 0);
	assert(decomp_calls == 1 && flush_calls == 1 && setup_calls == 1);

	/* Uncompressed copy ending exactly at the reservation: allowed. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	set_load(FB_BOTTOM - 0x100000);
	fake_images.os.image_len = 0x100000;
	rc = load();
	assert(rc == 0 && guard_msgs == 0);
	assert(decomp_calls == 1 && seen_decomp_comp == IH_COMP_NONE);

	/* Uncompressed copy whose last bytes reach into the reservation. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	set_load(FB_BOTTOM - 0x100000);
	fake_images.os.image_len = 0x100001;
	fake_images.os.end = SRC_ADDR + 0x200000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1);
	assert(decomp_calls == 0 && flush_calls == 0 && setup_calls == 0);
	assert(lmb_calls == 0 && bootstage_error_calls == 0);

	/* Uncompressed copy wholly inside the reservation: refused. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	set_load(FB_BOTTOM + 0x1000);
	fake_images.os.end = SRC_ADDR + 0x200000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && decomp_calls == 0);
	assert(flush_calls == 0 && setup_calls == 0 && memmove_calls == 0);

	/* An uncompressed image larger than the buffer is refused unwritten. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	set_load(0x1000);
	fake_images.os.image_len = UNC_LEN + 1;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && decomp_calls == 0);

	/* Compressed load far below the reservation: unchanged load. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	rc = load();
	assert(rc == 0 && guard_msgs == 0);
	assert(decomp_calls == 1);
	assert(seen_decomp_load == LOAD_ADDR);
	assert(seen_decomp_image_len == 0x100000);
	assert(seen_decomp_unc_len == UNC_LEN);
	assert(flush_calls == 1 && setup_calls == 1 && lmb_calls == 1);

	/*
	 * Compressed load whose declared output window reaches the
	 * reservation is refused before image_decomp() runs, even though the
	 * real output would have been smaller.
	 */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	set_load(FB_BOTTOM - 0x1000);
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1);
	assert(decomp_calls == 0 && flush_calls == 0 && setup_calls == 0);

	/* Window ending exactly at the reservation: allowed. */
	reset();
	video(FB_TOP, FB_TOP + 0x1000);
	fake_images.os.comp = IH_COMP_LZ4;
	set_load(FB_TOP - UNC_LEN);
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == 0 && guard_msgs == 0 && decomp_calls == 1);

	/* Reservation wholly above the window: allowed. */
	reset();
	video(FB_TOP + 0x10000, FB_TOP + 0x20000);
	fake_images.os.comp = IH_COMP_ZSTD;
	set_load(FB_TOP - UNC_LEN);
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == 0 && guard_msgs == 0 && decomp_calls == 1);

	/* One byte of the declared window inside the reservation: refused. */
	reset();
	video(FB_TOP - 0x1000, FB_TOP + 0x1000);
	fake_images.os.comp = IH_COMP_ZSTD;
	set_load(FB_TOP - UNC_LEN);
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && decomp_calls == 0);

	/* A compression type the guard cannot bound is refused unwritten. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_ZSTD + 1;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && decomp_calls == 0);

	/* Malformed reservations fail closed before any decompression write. */
	reset();
	video(0, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && decomp_calls == 0);
	reset();
	video(FB_BOTTOM, FB_BOTTOM);
	fake_images.os.comp = IH_COMP_GZIP;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && decomp_calls == 0);

	/*
	 * KERNEL_NOLOAD: the guard runs at the freshly allocated buffer with
	 * the full CONFIG_SYS_BOOTM_LEN window, because image_decomp() is told
	 * that much even though ALIGN(image_len * 4, SZ_1M) allocated less.
	 */
	reset();
	video(FB_TOP, FB_TOP + 0x1000);
	fake_images.os.comp = IH_COMP_GZIP;
	fake_images.os.type = IH_TYPE_KERNEL_NOLOAD;
	lmb_alloc_any_addr = FB_TOP - UNC_LEN;
	reloc_addr = FB_TOP - UNC_LEN;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == 0 && guard_msgs == 0);
	assert(lmb_any_calls == 1);
	assert(seen_lmb_any_size == ALIGN(0x100000 * 4, SZ_1M));
	assert(seen_decomp_load == FB_TOP - UNC_LEN);
	assert(seen_decomp_unc_len == UNC_LEN);
	assert(fake_images.ep == FB_TOP - UNC_LEN);
	/* Only the local copy is repointed; images->os.load is untouched. */
	assert(fake_images.os.load == LOAD_ADDR);
	assert(setup_calls == 1 && memmove_calls == 0);

	/* Same allocation, one byte of window inside the reservation. */
	reset();
	video(FB_TOP, FB_TOP + 0x1000);
	fake_images.os.comp = IH_COMP_GZIP;
	fake_images.os.type = IH_TYPE_KERNEL_NOLOAD;
	lmb_alloc_any_addr = FB_TOP - UNC_LEN + 1;
	reloc_addr = FB_TOP - UNC_LEN + 1;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && decomp_calls == 0);
	assert(lmb_any_calls == 1);

	/* Relocation into the reservation: refused after decompressing. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	reloc_addr = FB_BOTTOM + 0x1000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1);
	assert(decomp_calls == 1 && flush_calls == 1);
	assert(memmove_calls == 0 && lmb_calls == 0);
	assert(fake_images.ep == 0);

	/* Relocation ending exactly at the reservation: allowed. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	reloc_addr = FB_BOTTOM - 0x1000;
	setup_image_size = 0x1000;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == 0 && guard_msgs == 0);
	assert(memmove_calls == 1);
	assert(memmove_dst[0] == reloc_addr && memmove_len[0] == 0x1000);
	assert(lmb_calls == 1);

	/* Relocation crossing the reservation start: refused. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	reloc_addr = FB_BOTTOM - 0x800;
	setup_image_size = 0x1000;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && memmove_calls == 0);

	/* A zero-length relocation writes nothing, wherever it points. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	reloc_addr = FB_BOTTOM + 0x1000;
	setup_image_size = 0;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == 0 && guard_msgs == 0);
	assert(memmove_calls == 1 && memmove_len[0] == 0);

	/* A relocation whose end address would wrap is refused. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	reloc_addr = ULONG_MAX - 0x10;
	setup_image_size = 0x1000;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && memmove_calls == 0);

	/* Relocation to a malformed reservation: fail closed. */
	reset();
	video(0, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	reloc_addr = 0x40000000;
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && guard_msgs == 1 && memmove_calls == 0);

	/* The pre-existing legacy/new-format overwrite check is unchanged. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	set_load(0x41000000);
	fake_images.os.start = 0x41000000;
	fake_images.os.end = 0x41200000;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && puts_calls == 1 && guard_msgs == 0);
	assert(bootstage_error_calls == 1);
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	fake_images.legacy_hdr_valid = true;
	fake_images.legacy_hdr_os_copy.type = IH_TYPE_MULTI;
	set_load(0x41000000);
	fake_images.os.start = 0x41000000;
	fake_images.os.end = 0x41200000;
	rc = load();
	assert(rc == BOOTM_ERR_OVERLAP && puts_calls == 1 && guard_msgs == 0);

	/* A decompression failure and a booti_setup failure still propagate. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	decomp_ret = -ENOSPC;
	rc = load();
	assert(rc == 1 && decomp_calls == 1 && decomp_error_calls == 1);
	assert(flush_calls == 0 && setup_calls == 0);
	assert(guard_msgs == 0 && bootstage_error_calls == 1);
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	setup_ret = -EINVAL;
	rc = load();
	assert(rc == BOOTM_ERR_RESET && setup_calls == 1);
	assert(memmove_calls == 0 && guard_msgs == 0);

	/* A non-ARM64 image never takes the relocation path. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	fake_images.os.arch = 1;
	reloc_addr = FB_BOTTOM + 0x1000;
	rc = load();
	assert(rc == 0 && setup_calls == 0 && memmove_calls == 0);

	/* The post-load LMB reservation still runs on a permitted load. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_images.os.comp = IH_COMP_GZIP;
	set_load(0x1000);
	fake_images.os.end = SRC_ADDR + 0x1000;
	rc = load();
	assert(rc == 0 && lmb_calls == 1 && log_err_calls == 0);

	puts("bootm write call-site tests passed");
	return 0;
}
'''


def compile_and_run(tmp, name, source):
    src = Path(tmp) / f"{name}.c"
    exe = Path(tmp) / name
    src.write_text(source)
    subprocess.run(["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra",
                    "-Werror", "-Wno-unused-parameter", str(src), "-o", str(exe)],
                   check=True)
    subprocess.run([str(exe)], check=True)


def patched_bootm():
    """Apply the patch to a copy of the real source and return its text."""
    original = (SOURCE / "boot" / "bootm.c").read_text()
    # Anchors: the guards must stay attached to these exact writes.
    for anchor in (
            "err = image_decomp(os.comp, load, os.image_start, os.type,",
            "\t\t\tmemmove((void *)relocated_addr, load_buf, image_size);",
            "ret = booti_setup(load, &relocated_addr, &image_size, false);",
            "CONFIG_SYS_BOOTM_LEN, &load_end);"):
        if original.count(anchor) != 1:
            raise AssertionError(f"anchor changed in boot/bootm.c: {anchor}")
    with tempfile.TemporaryDirectory(prefix="arcadia-bootm-src-") as tmp:
        work = Path(tmp)
        (work / "boot").mkdir()
        shutil.copy(SOURCE / "boot" / "bootm.c", work / "boot" / "bootm.c")
        proc = subprocess.run(["patch", "-p1", "-s", "-i", str(PATCH)], cwd=work,
                              capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError(f"patch did not apply: {proc.stdout}{proc.stderr}")
        return (work / "boot" / "bootm.c").read_text()


def extract_function(text, signature):
    """Return a whole function body starting at signature, brace-counted."""
    start = text.index(signature)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unterminated function starting at {signature}")


def main():
    check_scope()
    write_check = added_function("arcadia_bootm_video_write_check")
    guard = added_function("arcadia_bootm_decomp_guard")
    helpers = write_check + "\n\n" + guard
    with tempfile.TemporaryDirectory(prefix="arcadia-bootm-test-") as tmp:
        compile_and_run(tmp, "helper_test", BASE_PRELUDE + helpers + HELPER_TESTS)
        if not (SOURCE / "boot" / "bootm.c").is_file():
            print(f"call-site tests skipped: no U-Boot source at {SOURCE}")
            return
        patched = patched_bootm()
        if extract_function(patched,
                            "static int arcadia_bootm_video_write_check(") != \
                write_check.strip():
            raise AssertionError("patched write check differs from the patch")
        if extract_function(patched,
                            "static int arcadia_bootm_decomp_guard(") != \
                guard.strip():
            raise AssertionError("patched decomp guard differs from the patch")
        load_os = extract_function(patched, STATIC_FUNC)
        compile_and_run(tmp, "callsite_test",
                        BASE_PRELUDE + CALLSITE_PRELUDE + helpers + "\n\n" +
                        load_os + CALLSITE_TESTS)


if __name__ == "__main__":
    main()
