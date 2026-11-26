# Radxa ZERO 3W: U-Boot boot artwork and display handoff

The default `u-boot-radxa` recipe selects **no** profile. On `MACHINE=radxa-zero3w`,
select **one** BitBake `-R` profile from this directory:

| Profile | Behavior |
|---|---|
| `artwork-cleanup.conf` | Draw `/arcadia-splash.bmp` from the SPL-selected MMC's `boot` partition (1280x720, 24-bit; a missing or invalid image is skipped). Normal video removal runs before Linux, so the display goes dark until Linux lights it. |
| `guarded-handoff.WIP.conf` | The same artwork plus an opt-in handoff: with `arcadia_handoff=yes` set, U-Boot keeps the VOP and HDMI running, reserves its framebuffer and publishes it to Linux through the FDT (the vendor kernel's loader-logo path then takes over without a black frame). Guards stop the extlinux read, `booti` and `bootm` from overwriting the framebuffer. Any guard failure falls back to normal video removal. |
| `guarded-handoff-auto.WIP.conf` | The guarded profile with `arcadia_handoff=yes` set in the default boot command, for firmware that is booted unattended (the environment is not persistent on this board). |

The handoff accepts only the tested RK3566 HDMI VP0 1080p60 mode; boot with
`video=HDMI-A-1:1920x1080@60` (already the extlinux default). The profiles are WIP: they do
not implement failure containment after the display has been preserved (see
`docs/radxa-reserved-framebuffer-handoff-plan.md` in the orchestrator).

`recipes-core/images/profiles/boot-artwork-staging.conf` adds `arcadia-splash.bmp`
(built by the `arcadia-boot-splash` recipe) to the Radxa development image; select it
separately when staging an image.

## Build and verify

```sh
bitbake -R <profile> -c compile u-boot-radxa
python3 tests/check_firmware_profile.py {cleanup|guarded} <u-boot.bin> <.config>   # marker check of the build
python3 tests/guard_helpers_test.py                                                 # host tests of the guards
```

The default recipe's BL31 is `rk3568_bl31_v1.46.elf` (`RADXA_BL31_FILE`); the earlier CPU3 BL31
stalled early in Linux boot when the boot was delayed.

## Test a build without flashing (RAM chainload)

From the installed U-Boot's prompt, with `u-boot-dtb.bin` on the rootfs and the SD's SPL, DDR
and BL31 untouched:

```
ext4load mmc 1:2 0x800000 /path/u-boot-dtb.bin        # 0x800000 = CONFIG_TEXT_BASE
fdt addr <0x800000 + size of u-boot-nodtb.bin>         # the DTB is appended to the image
fdt resize 1024
fdt set /chosen u-boot,spl-boot-device "/mmc@fe2b0000" # a chainloaded child does not inherit it
go 0x800000
```

A chainloaded child runs the real boot path, so a power cycle returns to the installed firmware.

## Stack and provenance

Stock U-Boot 2026.01 has no RK3568 VOP2 driver, so `vop2-base.inc` first applies the eight patches
of the [submitted VOP2/HDMI
series](https://lore.kernel.org/u-boot/cover.1784424101.git.daniel@makrotopia.org/). These are
`01-08-upstream.patch`: unmerged mailing-list changes by other authors (each header carries its
`Upstream-Status` link), not our work and not something we submit. Everything from `09` on is
local: the output-register fix, the artwork loader and the video config fragment. The guarded profiles add
the board handoff, `CONFIG_OF_BOARD_SETUP=y` and the three framebuffer write guards.
