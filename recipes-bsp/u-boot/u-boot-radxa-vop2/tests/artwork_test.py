#!/usr/bin/env python3
"""Host-side structural checks for the artwork patch and the guarded profile.

These checks do not exercise U-Boot, a filesystem, display hardware or DMA.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ArtworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patch = (ROOT / "arcadia-boot-artwork.patch").read_text()
        cls.conf = (ROOT / "guarded-handoff.conf").read_text()

    def test_artwork_patch_is_independent_of_handoff(self):
        self.assertNotIn("handoff_ready", self.patch)
        self.assertNotIn("arcadia_handoff", self.patch)
        self.assertIn("CONFIG_VIDEO_REMOVE=y", (ROOT / "vop2-video.cfg").read_text())

    def test_profile_patch_order_and_scope(self):
        patches = re.findall(r'file://(\S+\.patch)', self.conf)
        self.assertEqual(patches[-4:], [
            'radxa-guarded-logo-handoff.patch', '14-guard-extlinux-reads.patch',
            '15-booti-write-guards.patch', '16-bootm-write-guards.patch',
        ])
        self.assertIn('arcadia-boot-artwork.patch', patches)
        for name in ('IMAGE_BOOT_FILES', 'WKS_FILE_DEPENDS'):
            self.assertNotIn(name, self.conf)

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
