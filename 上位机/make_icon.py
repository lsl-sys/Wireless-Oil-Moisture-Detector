# -*- coding: utf-8 -*-
"""生成应用图标 app.ico（构建辅助脚本，打包前执行）"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "app.ico"
SIZE = 512


def draw(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 蓝色圆角方形底
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22),
                        fill=(18, 87, 214, 255))

    # 白色水滴：下方圆 + 上方三角尖
    cx, cy = size * 0.5, size * 0.62
    r = size * 0.235
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    d.polygon([(cx - r * 0.78, cy - r * 0.30),
               (cx + r * 0.78, cy - r * 0.30),
               (cx, size * 0.135)], fill=(255, 255, 255, 255))
    return img


def main():
    icon = draw(SIZE)
    icon.save(OUT, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("icon ->", OUT)


if __name__ == "__main__":
    main()
