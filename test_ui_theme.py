import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QStyleOption


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


class ThemeContrastTests(unittest.TestCase):
    def test_action_button_text_contrast_in_every_enabled_state(self):
        for name in ("PrimaryBtn", "SuccessBtn"):
            for state in ("", ":hover", ":pressed"):
                selector = "QPushButton#" + name + state
                with self.subTest(selector=selector):
                    rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", main.GLOBAL_QSS).group(1)
                    backgrounds = re.findall(r"stop:\d\s+(#[0-9A-Fa-f]{6})", rule)
                    self.assertTrue(backgrounds, "Action buttons must retain their gradient states")
                    # Both action styles use white labels; every gradient endpoint must be readable.
                    for background in backgrounds:
                        self.assertGreaterEqual(1.05 / (luminance(background) + 0.05), 4.5)


class ThemeResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def test_svg_icons_render_from_a_frozen_resource_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            for family in ("lucide", "uiverse"):
                shutil.copytree(Path(__file__).parent / "assets" / family, Path(folder) / "assets" / family)
            with patch.object(sys, "_MEIPASS", folder, create=True):
                main.ui_icon.cache_clear()
                try:
                    for name in ("save", "send", "file-archive", "cloud", "chevron-up", "chevron-down"):
                        with self.subTest(icon=name):
                            for mode in (main.QIcon.Normal, main.QIcon.Disabled):
                                image = main.ui_icon(name).pixmap(main.QSize(20, 20), mode).toImage()
                                self.assertFalse(image.isNull())
                                self.assertTrue(any(
                                    image.pixelColor(x, y).alpha() > 0
                                    for x in range(image.width()) for y in range(image.height())
                                ), "Bundled SVG must contain visible pixels")
                finally:
                    main.ui_icon.cache_clear()

    def test_popup_palette_uses_valid_opaque_colors(self):
        combo = main.SearchableComboBox()
        palette = combo.view().palette()
        for role in (main.QPalette.Base, main.QPalette.Window, main.QPalette.Text):
            self.assertTrue(palette.color(role).isValid())
            self.assertEqual(palette.color(role).alpha(), 255)
        self.assertLess(palette.color(main.QPalette.Base).lightness(), 70)
        self.assertGreater(palette.color(main.QPalette.Text).lightness(), 220)
        combo.deleteLater()

    def test_tree_chevrons_remain_centered_at_different_scales_and_row_heights(self):
        style = main.ClearTreeBranchStyle()
        for scale in (1, 1.25, 1.5):
            for height in (35, 36):
                for expanded in (False, True):
                    with self.subTest(scale=scale, height=height, expanded=expanded):
                        image = QImage(96, 96, QImage.Format_ARGB32_Premultiplied)
                        image.setDevicePixelRatio(scale)
                        image.fill(main.Qt.transparent)
                        option = QStyleOption()
                        option.rect = QRect(8, 6, 26, height)
                        option.state = main.QStyle.State_Children | main.QStyle.State_Enabled
                        if expanded:
                            option.state |= main.QStyle.State_Open
                        painter = main.QPainter(image)
                        style.drawPrimitive(main.QStyle.PE_IndicatorBranch, option, painter)
                        painter.end()
                        pixels = [(x, y) for x in range(image.width()) for y in range(image.height())
                                  if image.pixelColor(x, y).alpha() > 32]
                        self.assertTrue(pixels)
                        xs, ys = zip(*pixels)
                        center = main.QRectF(option.rect).center()
                        self.assertLessEqual(abs((min(xs) + max(xs) + 1) / 2 - center.x() * scale), 0.75)
                        self.assertLessEqual(abs((min(ys) + max(ys) + 1) / 2 - center.y() * scale), 0.75)
                        self.assertLessEqual(max(xs) - min(xs) + 1, 11 * scale)
                        self.assertLessEqual(max(ys) - min(ys) + 1, 11 * scale)


if __name__ == "__main__":
    unittest.main()
