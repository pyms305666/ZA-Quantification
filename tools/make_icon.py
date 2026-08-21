"""生成 ZA量化 图标：两个角度差 45° 的正方形（一个正放、一个旋转 45°）。

输出：assets/icon.png（256x256）、assets/icon.ico（多尺寸）、assets/favicon.png（64x64）。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent / "assets"
OUT_DIR.mkdir(exist_ok=True)

# 品牌色（与 UI 一致）
CYAN = (85, 183, 217, 255)
AMBER = (228, 173, 74, 255)
BG = (14, 17, 22, 255)  # 深色背景，与软件主题一致


def draw_square_icon(size: int) -> Image.Image:
    """画布 size x size：背景圆角深色块 + 两个角度差 45° 的正方形线框。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 深色圆角背景（四角透明，主体方中带圆）
    radius = int(size * 0.18)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BG)

    center = size / 2
    # 正方形半边长（占画布 ~55%）
    half = size * 0.30
    # 旋转 45° 后顶点的外接半径（同一正方形旋转后四个角落在 (±r, 0)/(0, ±r)）
    r = half * math.sqrt(2)

    def polygon(points: list[tuple[float, float]], outline: tuple[int, int, int, int],
                width: float, fill=None) -> None:
        draw.polygon(points, outline=outline, fill=fill)
        # polygon 的 outline 宽度在 PIL 中受限，用逐边 line 补粗
        for i in range(len(points)):
            a, b = points[i], points[(i + 1) % len(points)]
            draw.line([a, b], fill=outline, width=max(1, int(width)))

    # 正方形 1：正放（角度 0°），青色描边 + 半透明填充
    square_0 = [
        (center - half, center - half), (center + half, center - half),
        (center + half, center + half), (center - half, center + half),
    ]
    polygon(square_0, outline=CYAN, width=size * 0.045,
            fill=(85, 183, 217, 46))

    # 正方形 2：旋转 45°，金色描边 + 半透明填充
    square_45 = [
        (center - r, center), (center, center - r),
        (center + r, center), (center, center + r),
    ]
    polygon(square_45, outline=AMBER, width=size * 0.045,
            fill=(228, 173, 74, 40))

    return image


def main() -> None:
    icon_256 = draw_square_icon(256)
    icon_256.save(OUT_DIR / "icon.png")
    # 多尺寸 ICO
    icon_256.save(OUT_DIR / "icon.ico", format="ICO",
                  sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                         (128, 128), (256, 256)])
    favicon = draw_square_icon(64)
    favicon.save(OUT_DIR / "favicon.png")
    print(f"图标已生成：{OUT_DIR}")


if __name__ == "__main__":
    main()
