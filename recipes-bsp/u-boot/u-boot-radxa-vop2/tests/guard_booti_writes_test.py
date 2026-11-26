#!/usr/bin/env python3
"""Compile and exercise the booti framebuffer pre-write guards from the WIP patch.

Three layers run here:

1. Patch-scope checks: 15-booti-write-guards.WIP.patch must touch only
   cmd/booti.c, must add no #include, and must contain no removed line, so
   every existing input/load/source check in the file is unchanged.
2. Helper tests compile the exact added body of arcadia_video_write_check()
   (extracted from the patch) against a fake global_data, and check that the
   same body is what the patch actually installs into cmd/booti.c.
3. Call-site tests copy the real cmd/booti.c, apply the patch to that copy,
   extract the patched booti_start() and compile it with stubs for
   image_decomp(), booti_setup(), memmove(), env_get_ulong(), LMB and the
   bootm states. They assert that no decompression, copy-back or relocation
   write happens when the guard rejects the destination, and that a permitted
   boot still reaches the same stubs in the same order. This layer is skipped,
   with a message, when the U-Boot source tree is unavailable (set
   ARCADIA_UBOOT_SOURCE to override the default /tmp/arcadia-uboot-vop2/source).

None of this tests a real decompressor, libfdt, an actual bootstd/booti run,
the contents of the video reservation, LMB bookkeeping, or hardware. Not
covered by the patch at all: FIT/bootm payload loads, raw load/acp commands,
initrd and FDT placement, EFI loaders, other bootmeths and an active IOMMU
during handoff. It needs a host C compiler.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

RECIPE = Path(__file__).resolve().parents[1]
PATCH = RECIPE / "15-booti-write-guards.WIP.patch"
SOURCE = Path(os.environ.get("ARCADIA_UBOOT_SOURCE", "/tmp/arcadia-uboot-vop2/source"))


def patch_files():
    """Return the set of files the patch claims to modify."""
    return {line.split()[1][2:] for line in PATCH.read_text().splitlines()
            if line.startswith("--- a/")}


def check_scope():
    """The patch must only touch cmd/booti.c and only add lines."""
    files = patch_files()
    if files != {"cmd/booti.c"}:
        raise AssertionError(f"patch scope is {sorted(files)}")
    lines = PATCH.read_text().splitlines()
    # Everything before the bare --- separator is the commit message.
    body = lines[lines.index("---") + 1:]
    if any(re.match(r"\+\s*#\s*include", line) for line in body):
        raise AssertionError("patch adds an include; only guards are expected")
    removed = [line for line in body
               if line.startswith("-") and not line.startswith("--- a/")]
    if removed:
        raise AssertionError(f"patch removes existing lines: {removed}")
    if body[0] != "--- a/cmd/booti.c":
        raise AssertionError(f"unexpected first hunk header {body[0]!r}")


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


HELPER_PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

typedef unsigned long ulong;

struct fake_gd { ulong video_bottom, video_top, ram_base, ram_top; };
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
	return arcadia_video_write_check(addr, size);
}

int main(void)
{
	/* No recorded reservation (also the built-in case for !CONFIG_VIDEO). */
	video(0, 0);
	assert(check(0, 0) == 0);
	assert(check(0x1000, 0x1000) == 0);
	assert(check(0, ULONG_MAX) == 0);
	assert(check(ULONG_MAX, 1) == 0);
	assert(check(FB_BOTTOM, FB_TOP - FB_BOTTOM) == 0);

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

	/* Adjacent: the write ends exactly at bottom or starts exactly at top. */
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
	assert(check(0, FB_TOP) == -EPERM);

	/* Wrapping end address is rejected before any range comparison. */
	assert(check(ULONG_MAX - 1, 4) == -ERANGE);
	assert(check(ULONG_MAX, 1) == -ERANGE);
	assert(check(FB_BOTTOM, ULONG_MAX - FB_BOTTOM + 2) == -ERANGE);
	assert(check(0, ULONG_MAX) != -ERANGE);

	puts("booti write helper tests passed");
	return 0;
}
'''

CALLSITE_PRELUDE = r'''
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
typedef unsigned int u32;
typedef unsigned long long u64;
typedef unsigned long long phys_addr_t;
typedef unsigned long long phys_size_t;
typedef unsigned char uint8_t;

struct image_info { ulong start, end; };
struct bootm_headers { struct image_info os; ulong ep; };
struct bootm_info {
	const char *addr_img;
	const char *conf_ramdisk;
	const char *conf_fdt;
	bool boot_progress;
	struct bootm_headers *images;
	const char *cmd_name;
	int argc;
	char *const *argv;
};

enum { BOOTM_STATE_START = 1, IH_TYPE_KERNEL = 2 };
enum lmb_mem_type { LMB_MEM_ALLOC_ADDR = 1 };
#define LMB_NONE 0

struct fake_gd { ulong video_bottom, video_top, ram_base, ram_top; };
static struct fake_gd fake_gd;

#define gd (&fake_gd)
#define gd_video_bottom() (gd->video_bottom)
#define gd_video_top() (gd->video_top)
#define debug(...) do { } while (0)

/* Knobs the test sets before calling booti_start(). */
static ulong image_load_addr;
static int ctype_value;
static ulong env_dest, env_comp_len;
static int decomp_ret;
static ulong decomp_out_len;
static ulong reloc_addr, setup_image_size;
static int setup_ret;

/* Observations. */
static int decomp_calls, booti_setup_calls, lmb_calls, find_images_calls;
static int printf_calls, guard_msgs, puts_calls, run_states_calls;
static ulong seen_decomp_dest, seen_decomp_unc_len, seen_decomp_image_len;
static int memmove_calls;
static ulong memmove_dst[4], memmove_len[4];

static void *map_sysmem(ulong paddr, unsigned long size)
{
	(void)size;
	return (void *)paddr;
}

static void unmap_sysmem(const void *vaddr)
{
	(void)vaddr;
}

static ulong hextoul(const char *str, char **endp)
{
	(void)str;
	(void)endp;
	return 0;
}

static int image_decomp_type(const unsigned char *buf, ulong len)
{
	(void)buf;
	(void)len;
	return ctype_value;
}

static ulong env_get_ulong(const char *name, int base, ulong default_val)
{
	(void)base;
	if (!strcmp(name, "kernel_comp_addr_r"))
		return env_dest;
	if (!strcmp(name, "kernel_comp_size"))
		return env_comp_len;
	return default_val;
}

static int image_decomp(int comp, ulong load, ulong image_start, int type,
			void *load_buf, void *image_buf, ulong image_len,
			uint unc_len, ulong *load_end)
{
	(void)comp;
	(void)image_start;
	(void)type;
	(void)image_buf;
	decomp_calls++;
	seen_decomp_dest = (ulong)load_buf;
	seen_decomp_unc_len = unc_len;
	seen_decomp_image_len = image_len;
	*load_end = load + decomp_out_len;
	return decomp_ret;
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
	if (strstr(fmt, "Arcadia booti guard"))
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

static int bootm_run_states(struct bootm_info *bmi, int states)
{
	(void)bmi;
	(void)states;
	run_states_calls++;
	return 0;
}

static int booti_setup(ulong image, ulong *relocated_addr, ulong *size,
		       bool force_reloc)
{
	(void)image;
	(void)force_reloc;
	booti_setup_calls++;
	*relocated_addr = reloc_addr;
	*size = setup_image_size;
	return setup_ret;
}

static int lmb_alloc_mem(enum lmb_mem_type type, u64 align, phys_addr_t *addr,
			 phys_size_t size, u32 flags)
{
	(void)type;
	(void)align;
	(void)addr;
	(void)size;
	(void)flags;
	lmb_calls++;
	return 0;
}

static u32 le32_to_cpu(u32 val)
{
	return val;
}

static int bootm_find_images(ulong img_addr, const char *conf_ramdisk,
			     const char *conf_fdt, ulong start, ulong size)
{
	(void)img_addr;
	(void)conf_ramdisk;
	(void)conf_fdt;
	(void)start;
	(void)size;
	find_images_calls++;
	return 0;
}
'''

STATIC_BOOTI = "static int booti_start(struct bootm_info *bmi)"

CALLSITE_TESTS = r'''
/* Drop the puts() redirection so the test can report success itself. */
#undef puts

#define FB_BOTTOM 0x7e000000UL
#define FB_TOP    0x80000000UL
#define LD_ADDR   0x40200000UL
#define DEST_ADDR 0x50000000UL

static struct bootm_headers fake_images;
static struct bootm_info bmi = { NULL, "-", NULL, true, &fake_images, "booti",
				 0, NULL };

static void reset(void)
{
	fake_gd.video_bottom = 0;
	fake_gd.video_top = 0;
	fake_gd.ram_base = 0x100000;
	fake_gd.ram_top = 0xf0000000UL;
	image_load_addr = LD_ADDR;
	ctype_value = 1;
	env_dest = DEST_ADDR;
	env_comp_len = 0x1000;
	decomp_ret = 0;
	decomp_out_len = 0x2000;
	reloc_addr = LD_ADDR;
	setup_image_size = 0x1000;
	setup_ret = 0;
	decomp_calls = booti_setup_calls = lmb_calls = find_images_calls = 0;
	printf_calls = guard_msgs = puts_calls = run_states_calls = 0;
	seen_decomp_dest = seen_decomp_unc_len = seen_decomp_image_len = 0;
	memmove_calls = 0;
	memset(memmove_dst, 0, sizeof(memmove_dst));
	memset(memmove_len, 0, sizeof(memmove_len));
	fake_images.ep = 0;
	fake_images.os.start = fake_images.os.end = 0;
}

static void video(ulong bottom, ulong top)
{
	fake_gd.video_bottom = bottom;
	fake_gd.video_top = top;
}

/*
 * A stub memmove must never have been handed a video-overlapping range. A
 * malformed range means the guard fails closed, so no write may happen at all.
 */
static void assert_no_video_write(void)
{
	ulong bottom = fake_gd.video_bottom, top = fake_gd.video_top;
	int i;

	/* No reservation recorded: these writes are unaffected. */
	if (!bottom && !top)
		return;
	/* Malformed range: the guard must have refused every write. */
	if (!bottom || top <= bottom) {
		assert(memmove_calls == 0);
		return;
	}
	for (i = 0; i < memmove_calls && i < 4; i++) {
		ulong dst = memmove_dst[i], n = memmove_len[i];

		assert(!n || dst >= top || dst + n <= bottom);
	}
}

/* Route every case through here so the invariant is checked each time. */
static int run_booti(void)
{
	int (*const start)(struct bootm_info *) = booti_start;
	int rc = start(&bmi);

	assert_no_video_write();
	return rc;
}

int main(void)
{
	int rc;

	/* Uncompressed, no reservation: the legacy path is untouched. */
	reset();
	ctype_value = 0;
	rc = run_booti();
	assert(rc == 0);
	assert(run_states_calls == 1 && decomp_calls == 0);
	assert(memmove_calls == 0);          /* relocated_addr == ld */
	assert(booti_setup_calls == 1 && lmb_calls == 1 && find_images_calls == 1);
	assert(guard_msgs == 0);
	assert(fake_images.ep == LD_ADDR);
	assert(fake_images.os.start == LD_ADDR);
	assert(fake_images.os.end == LD_ADDR + setup_image_size);

	/* Uncompressed, relocation wholly below the reservation: allowed. */
	reset();
	ctype_value = 0;
	video(FB_BOTTOM, FB_TOP);
	reloc_addr = FB_BOTTOM - 0x1000;
	setup_image_size = 0x1000;
	rc = run_booti();
	assert(rc == 0 && guard_msgs == 0);
	assert(memmove_calls == 1);
	assert(memmove_dst[0] == reloc_addr && memmove_len[0] == setup_image_size);
	assert(lmb_calls == 1 && find_images_calls == 1);

	/* Uncompressed, relocation starting exactly at video_top: allowed. */
	reset();
	ctype_value = 0;
	video(FB_BOTTOM, FB_TOP);
	reloc_addr = FB_TOP;
	rc = run_booti();
	assert(rc == 0 && memmove_calls == 1);

	/* Uncompressed, relocation inside the reservation: refused, no write. */
	reset();
	ctype_value = 0;
	video(FB_BOTTOM, FB_TOP);
	reloc_addr = FB_BOTTOM + 0x1000;
	rc = run_booti();
	assert(rc == -EPERM && guard_msgs == 1);
	assert(memmove_calls == 0 && lmb_calls == 0 && find_images_calls == 0);
	assert(booti_setup_calls == 1);

	/* Uncompressed, relocation crossing video_bottom: refused. */
	reset();
	ctype_value = 0;
	video(FB_BOTTOM, FB_TOP);
	reloc_addr = FB_BOTTOM - 0x800;
	setup_image_size = 0x1000;
	rc = run_booti();
	assert(rc == -EPERM && memmove_calls == 0 && find_images_calls == 0);

	/* Relocation whose end address would wrap: refused before writing. */
	reset();
	ctype_value = 0;
	video(FB_BOTTOM, FB_TOP);
	reloc_addr = ULONG_MAX - 0x10;
	setup_image_size = 0x1000;
	rc = run_booti();
	assert(rc == -ERANGE && memmove_calls == 0 && find_images_calls == 0);

	/*
	 * Malformed reserved range fails closed before any write. A boot that
	 * performs none of these three writes is unaffected: with no relocation
	 * and an uncompressed Image there is nothing to check.
	 */
	reset();
	ctype_value = 0;
	video(0, FB_TOP);
	reloc_addr = 0x40000000;
	rc = run_booti();
	assert(rc == -ERANGE && memmove_calls == 0);
	reset();
	ctype_value = 0;
	video(FB_TOP, FB_BOTTOM);
	reloc_addr = 0x40000000;
	rc = run_booti();
	assert(rc == -ERANGE && memmove_calls == 0);
	reset();
	ctype_value = 0;
	video(FB_BOTTOM, FB_BOTTOM);
	reloc_addr = 0x40000000;
	rc = run_booti();
	assert(rc == -ERANGE && memmove_calls == 0);

	/* With no write to make, a malformed range does not block the boot. */
	reset();
	ctype_value = 0;
	video(FB_TOP, FB_BOTTOM);
	rc = run_booti();
	assert(rc == 0 && memmove_calls == 0 && guard_msgs == 0);

	/* Compressed, no reservation: decompress and copy-back run as before. */
	reset();
	rc = run_booti();
	assert(rc == 0 && guard_msgs == 0);
	assert(decomp_calls == 1);
	assert(seen_decomp_dest == DEST_ADDR);
	assert(seen_decomp_unc_len == 0x1000UL * 10);
	assert(seen_decomp_image_len == 0x1000);
	assert(memmove_calls == 1);
	assert(memmove_dst[0] == LD_ADDR && memmove_len[0] == decomp_out_len);
	assert(booti_setup_calls == 1 && lmb_calls == 1 && find_images_calls == 1);

	/* Compressed, destination window inside the reservation: refused. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	env_dest = FB_BOTTOM + 0x1000;
	env_comp_len = 0x100;
	rc = run_booti();
	assert(rc == -EPERM && guard_msgs == 1);
	assert(decomp_calls == 0 && memmove_calls == 0);
	assert(booti_setup_calls == 0 && lmb_calls == 0 && find_images_calls == 0);

	/* Compressed, destination window crossing video_bottom: refused. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	env_dest = FB_BOTTOM - 0x800;
	env_comp_len = 0x100;		/* window is 0xa00 bytes */
	rc = run_booti();
	assert(rc == -EPERM && decomp_calls == 0 && memmove_calls == 0);

	/* Destination window ending exactly at video_bottom: allowed. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	env_dest = FB_BOTTOM - 0xa00;
	env_comp_len = 0x100;
	rc = run_booti();
	assert(rc == 0 && decomp_calls == 1 && memmove_calls == 1);

	/* Destination window starting exactly at video_top: allowed. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	env_dest = FB_TOP;
	env_comp_len = 0x100;
	rc = run_booti();
	assert(rc == 0 && decomp_calls == 1);

	/* A comp_len whose 10x bound wraps is refused before decompressing. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	env_comp_len = ULONG_MAX / 10 + 1;
	assert(env_comp_len * 10 < env_comp_len);	/* the multiply does wrap */
	rc = run_booti();
	assert(rc == -ERANGE && guard_msgs == 1);
	assert(decomp_calls == 0 && memmove_calls == 0 && booti_setup_calls == 0);

	/*
	 * At the exact non-wrapping boundary the multiply succeeds, so decomp_len
	 * is still huge and nearly ULONG_MAX; any real dest then wraps and the
	 * window check refuses. Had the multiply overflowed instead, decomp_len
	 * would be a few bytes and image_decomp() would have run.
	 */
	reset();
	video(FB_BOTTOM, FB_TOP);
	env_comp_len = ULONG_MAX / 10;
	rc = run_booti();
	assert(rc == -ERANGE && guard_msgs == 1 && decomp_calls == 0);

	/* A large scaled window that does not wrap still covers video. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_gd.ram_base = 0;
	env_dest = 0x1000;
	env_comp_len = 0x10000000;
	rc = run_booti();
	assert(rc == -EPERM && guard_msgs == 1 && decomp_calls == 0);

	/* Defensive wrap branch: reachable only if dest could be near ULONG_MAX. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_gd.ram_top = ULONG_MAX;
	env_dest = ULONG_MAX - 8;
	env_comp_len = 0x100;
	rc = run_booti();
	assert(rc == -ERANGE && decomp_calls == 0 && memmove_calls == 0);

	/* Decompression window fine, copy-back to ld inside video: refused. */
	reset();
	video(LD_ADDR, LD_ADDR + 0x10000);
	rc = run_booti();
	assert(rc == -EPERM && guard_msgs == 1);
	assert(decomp_calls == 1);		/* decompression did write dest */
	assert(memmove_calls == 0);		/* but nothing was copied back */
	assert(booti_setup_calls == 0 && find_images_calls == 0);

	/* Copy-back ending exactly at video_bottom: allowed. */
	reset();
	video(LD_ADDR + 0x2000, LD_ADDR + 0x10000);
	decomp_out_len = 0x2000;
	rc = run_booti();
	assert(rc == 0 && guard_msgs == 0);
	assert(memmove_calls == 1 && memmove_dst[0] == LD_ADDR);
	assert(memmove_len[0] == decomp_out_len);

	/* Copy-back crossing video_bottom: refused. */
	reset();
	video(LD_ADDR + 0x1fff, LD_ADDR + 0x10000);
	decomp_out_len = 0x2000;
	rc = run_booti();
	assert(rc == -EPERM && memmove_calls == 0 && booti_setup_calls == 0);

	/* Copy-back whose end address would wrap: refused. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	image_load_addr = ULONG_MAX - 0x10;
	env_dest = DEST_ADDR;
	decomp_out_len = 0x2000;
	rc = run_booti();
	assert(rc == -ERANGE && memmove_calls == 0 && booti_setup_calls == 0);

	/* Zero-length copy-back writes nothing, so it stays allowed. */
	reset();
	video(LD_ADDR, LD_ADDR + 0x10000);
	decomp_out_len = 0;
	rc = run_booti();
	assert(rc == 0 && memmove_calls == 1 && memmove_len[0] == 0);

	/* No recorded reservation: decompressed path is unaffected. */
	reset();
	env_dest = FB_BOTTOM + 0x1000;
	env_comp_len = 0x100;
	reloc_addr = FB_BOTTOM + 0x2000;
	rc = run_booti();
	assert(rc == 0 && guard_msgs == 0);
	assert(decomp_calls == 1 && memmove_calls == 2);
	assert(memmove_dst[1] == reloc_addr);

	/* Existing input checks are unchanged and still come first. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	env_dest = 0;
	rc = run_booti();
	assert(rc == -EINVAL && puts_calls == 1);
	assert(decomp_calls == 0 && guard_msgs == 0);
	reset();
	video(FB_BOTTOM, FB_TOP);
	fake_gd.ram_top = 0x40000000;
	rc = run_booti();
	assert(rc == -EINVAL && puts_calls == 1 && decomp_calls == 0);

	/* Decompressor and booti_setup failures are propagated unchanged. */
	reset();
	video(FB_BOTTOM, FB_TOP);
	decomp_ret = -ENOSPC;
	rc = run_booti();
	assert(rc == -ENOSPC && decomp_calls == 1 && memmove_calls == 0);
	reset();
	video(FB_BOTTOM, FB_TOP);
	setup_ret = -EINVAL;
	rc = run_booti();
	assert(rc == 1 && booti_setup_calls == 1);
	assert(memmove_calls == 1);		/* only the decompression copy-back */
	assert(find_images_calls == 0);

	puts("booti write call-site tests passed");
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


def patched_booti():
    """Apply the patch to a copy of the real source and return its text."""
    original = (SOURCE / "cmd" / "booti.c").read_text()
    # Anchors: the guard must stay attached to these exact writes.
    for anchor in ("image_decomp(ctype, 0, ld, IH_TYPE_KERNEL,",
                   "memmove((void *) ld, (void *)dest, dest_end);",
                   "memmove((void *)relocated_addr, (void *)ld, image_size);",
                   "decomp_len = comp_len * 10;"):
        if original.count(anchor) != 1:
            raise AssertionError(f"anchor changed in cmd/booti.c: {anchor}")
    with tempfile.TemporaryDirectory(prefix="arcadia-booti-src-") as tmp:
        work = Path(tmp)
        (work / "cmd").mkdir()
        shutil.copy(SOURCE / "cmd" / "booti.c", work / "cmd" / "booti.c")
        proc = subprocess.run(["patch", "-p1", "-s", "-i", str(PATCH)], cwd=work,
                              capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError(f"patch did not apply: {proc.stdout}{proc.stderr}")
        return (work / "cmd" / "booti.c").read_text()


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
    helper = added_function("arcadia_video_write_check")
    with tempfile.TemporaryDirectory(prefix="arcadia-booti-test-") as tmp:
        compile_and_run(tmp, "helper_test", HELPER_PRELUDE + helper + HELPER_TESTS)
        if not (SOURCE / "cmd" / "booti.c").is_file():
            print(f"call-site tests skipped: no U-Boot source at {SOURCE}")
            return
        patched = patched_booti()
        installed = extract_function(patched, "static int arcadia_video_write_check(")
        if installed != helper.strip():
            raise AssertionError("patched helper differs from the patch addition")
        booti_start = extract_function(patched, STATIC_BOOTI)
        compile_and_run(tmp, "callsite_test",
                        CALLSITE_PRELUDE + helper + "\n\n" + booti_start +
                        CALLSITE_TESTS)


if __name__ == "__main__":
    main()
