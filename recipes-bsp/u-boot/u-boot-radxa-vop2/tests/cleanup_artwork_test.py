#!/usr/bin/env python3
"""Host-side structural checks for the artwork-cleanup profile.

These checks do not exercise U-Boot, a filesystem, display hardware or DMA.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class CleanupArtworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patch = (ROOT / "arcadia-boot-artwork.patch").read_text()
        cls.conf = (ROOT / "artwork-cleanup.conf").read_text() + (ROOT / "vop2-base.inc").read_text()

    def test_no_handoff_patch_and_cleanup_enabled(self):
        self.assertNotIn("radxa-guarded-logo-handoff", self.conf)
        self.assertNotIn("vop2-handoff.WIP.cfg", self.conf)
        self.assertIn("file://arcadia-boot-artwork.patch", self.conf)
        self.assertNotIn("handoff_ready", self.patch)
        self.assertNotIn("arcadia_handoff", self.patch)
        self.assertIn("CONFIG_VIDEO_REMOVE=y", (ROOT / "vop2-video.cfg").read_text())

    def test_profiles_keep_cleanup_independent_and_guards_together(self):
        cleanup = (ROOT / 'artwork-cleanup.conf').read_text()
        handoff = (ROOT / 'guarded-handoff.WIP.conf').read_text()
        base = (ROOT / 'vop2-base.inc').read_text()
        for profile in (cleanup, handoff):
            self.assertIn('require vop2-base.inc', profile.splitlines())
        self.assertFalse(re.findall(r'file://(\S+\.patch)', cleanup))
        self.assertNotIn('handoff', base)
        self.assertEqual(re.findall(r'file://(\S+\.patch)', handoff), [
            'radxa-guarded-logo-handoff.WIP.patch', '14-guard-extlinux-reads.WIP.patch',
            '15-booti-write-guards.WIP.patch', '16-bootm-write-guards.WIP.patch',
        ])
        for text in (base, cleanup, handoff):
            self.assertNotIn('IMAGE_BOOT_FILES', text)
            self.assertNotIn('WKS_FILE_DEPENDS', text)

    def test_boot_medium_lookup_fails_closed(self):
        for required in ('"u-boot,spl-boot-device"', '"boot"',
                         'if (!path)', 'if (part < 1)',
                         'fs_set_blk_dev_with_part(desc, part)'):
            self.assertIn(required, self.patch)
        self.assertNotIn('fs_set_blk_dev("mmc"', self.patch)

    def test_bounded_artwork_and_nonfatal_failure(self):
        for required in ('const size_t size = 2764854',
                         'fs_read(path, map_to_sysmem(data), 0, size + 1, &actual)',
                         'actual != size', 'data[0] != \'B\'',
                         'get_unaligned_le32(data + 18) != 1280',
                         'get_unaligned_le32(data + 22) != 720',
                         'BMP_ALIGN_CENTER', 'free(data);'):
            self.assertIn(required, self.patch)
        self.assertIn('continuing without it', self.patch)


if __name__ == '__main__':
    unittest.main()
