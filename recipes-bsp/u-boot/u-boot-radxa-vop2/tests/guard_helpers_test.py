#!/usr/bin/env python3
"""Compile and exercise guard helpers and board hooks from the WIP patch.

The tiny fake LMB/FDT objects test decision logic, not libfdt, actual hook
invocation in U-Boot, hardware or IOMMU behavior. Requires a host C compiler.
"""
from pathlib import Path
import subprocess
import tempfile

PATCH = Path(__file__).resolve().parents[1] / "radxa-guarded-logo-handoff.WIP.patch"


def added_function(name):
    lines = PATCH.read_text().splitlines()
    prefix = "+static bool " + name + "("
    start = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    body = []
    for line in lines[start:]:
        if not line.startswith("+") or line.startswith("+++"):
            raise AssertionError(f"{name}: helper is not a contiguous patch addition")
        body.append(line[1:])
        if line == "+}":
            return "\n".join(body)
    raise AssertionError(f"{name}: unterminated")


def patched_board_functions():
    lines = PATCH.read_text().splitlines()
    marker = "--- a/board/radxa/zero3-rk3566/zero3-rk3566.c"
    start = lines.index(marker) + 2
    section = []
    for line in lines[start:]:
        if line.startswith("--- a/"):
            break
        if line.startswith("@@") or line.startswith("-"):
            continue
        if line.startswith(("+", " ")):
            section.append(line[1:])
    start = section.index("struct bd_info;")
    end = next(i for i in range(section.index("int ft_board_setup(void *blob, struct bd_info *bd)"),
                                len(section)) if section[i] == "}")
    return "\n".join(section[start:end + 1])


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
typedef unsigned long ulong;
#define LMB_NOOVERWRITE 1
#define FDT_ERR_NOTFOUND 1
struct lmb_region { ulong base, size; unsigned flags; };
struct list { void *data; int count; };
struct lmb { struct list used_mem; };
static struct lmb map;
static struct lmb *lmb_get(void) { return &map; }
struct fake_fdt { const char *args, *status; int args_len, status_len; bool chosen; };
static bool mock_ready;
static int mock_env, mock_calls;
static int env_get_yesno(const char *name) { (void)name; return mock_env; }
int rk_vop2_arcadia_handoff(void *blob) { (void)blob; mock_calls++; mock_ready=true; return 0; }
void rk_vop2_arcadia_handoff_reset(void) { mock_ready=false; }
static int fdt_path_offset(const void *b, const char *p) {
    const struct fake_fdt *f=b; (void)p; return f->chosen ? 1 : -1;
}
static const void *fdt_getprop(const void *b, int node, const char *name, int *len) {
    const struct fake_fdt *f=b; (void)node;
    if (!strcmp(name, "bootargs")) { *len=f->args ? f->args_len : -FDT_ERR_NOTFOUND; return f->args; }
    *len=f->status ? f->status_len : -FDT_ERR_NOTFOUND; return f->status;
}
'''

TESTS = r'''
static bool mode(const char *s) {
    struct fake_fdt f = { .chosen=true, .args=s, .args_len=(int)strlen(s)+1 };
    return arcadia_linux_mode_matches(&f);
}
int main(void) {
    struct lmb_region r[] = {{0x1000,0x1000,LMB_NOOVERWRITE},
                             {0x3000,0x1000,LMB_NOOVERWRITE},
                             {0x5000,0x1000,0}};
    struct fake_fdt f = { .chosen=true };
    const char embedded[] = "root=/dev/a\0video=HDMI-A-1:1920x1080@60";
    map.used_mem.data=r; map.used_mem.count=3;
    assert(arcadia_fb_reserved(0x1000,0x1000));
    assert(arcadia_fb_reserved(0x1100,0x100));
    assert(!arcadia_fb_reserved(0x1000,0));
    assert(!arcadia_fb_reserved(0x0fff,2));
    assert(!arcadia_fb_reserved(0x1fff,2));
    assert(!arcadia_fb_reserved(0x1000,0x3000));
    assert(!arcadia_fb_reserved(0x5000,1));
    assert(!arcadia_fb_reserved(ULONG_MAX-1,4));
    assert(mode("root=/dev/x video=HDMI-A-1:1920x1080@60 splash"));
    assert(mode("  video=HDMI-A-1:1920x1080@60  "));
    assert(!mode("root=/dev/x"));
    assert(!mode("video=HDMI-A-1:1280x720@60"));
    assert(!mode("video=HDMI-A-1:1920x1080@60 video=HDMI-A-1:1280x720@60"));
    assert(!mode("video=HDMI-A-1:1920x1080@60 video=HDMI-A-1:1920x1080@60"));
    assert(!mode("-- video=HDMI-A-1:1920x1080@60"));
    assert(!mode("video=HDMI-A-1:1920x1080@60 -- initarg"));
    assert(!mode("video=HDMI-A-1:1920x1080@60\tvideo=HDMI-A-1:1280x720@60"));
    assert(!mode("\"video=HDMI-A-1:1920x1080@60\""));
    assert(!mode("video=HDMI-A-1:1920x1080@60foo"));
    f.args=embedded; f.args_len=sizeof(embedded); assert(!arcadia_linux_mode_matches(&f));
    f.chosen=false; assert(!arcadia_linux_mode_matches(&f));
    f.status=NULL; assert(arcadia_fdt_enabled(&f,1));
    f.status="ok"; f.status_len=3; assert(arcadia_fdt_enabled(&f,1));
    f.status="okay"; f.status_len=5; assert(arcadia_fdt_enabled(&f,1));
    f.status="disabled"; f.status_len=9; assert(!arcadia_fdt_enabled(&f,1));
    f.status="okay"; f.status_len=4; assert(!arcadia_fdt_enabled(&f,1));
    mock_env=1; mock_ready=true;
    assert(ft_board_setup(NULL,NULL)==0 && !mock_ready && mock_calls==0);
    mock_ready=true; board_bootm_start(); assert(!mock_ready);
    assert(ft_board_setup(NULL,NULL)==0 && mock_ready && mock_calls==1);
    board_bootm_start(); assert(!mock_ready); /* next boot may skip FDT fixups */
    assert(ft_board_setup(NULL,NULL)==0 && mock_ready && mock_calls==2);
    board_bootm_cancel(); assert(!mock_ready);
    assert(ft_board_setup(NULL,NULL)==0 && !mock_ready && mock_calls==2);
    board_bootm_start(); mock_env=0; mock_ready=true;
    assert(ft_board_setup(NULL,NULL)==0 && !mock_ready && mock_calls==2);
    puts("guard helper tests passed");
    return 0;
}
'''


def main():
    helpers = "\n\n".join(added_function(name) for name in (
        "arcadia_fb_reserved", "arcadia_fdt_enabled", "arcadia_linux_mode_matches"))
    with tempfile.TemporaryDirectory(prefix="arcadia-handoff-test-") as tmp:
        src = Path(tmp) / "guard_test.c"
        exe = Path(tmp) / "guard_test"
        src.write_text(PRELUDE + helpers + "\n" + patched_board_functions() + TESTS)
        subprocess.run(["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra",
                        "-Werror", "-Wno-unused-parameter", str(src), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
