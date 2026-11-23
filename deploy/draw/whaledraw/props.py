"""现成的道具零件：画一次，到处用。每个函式都收 Pen（座标写最终尺寸），画在呼叫它的那一层。

- pumpkin(pen, cx, cy, r)          圆润的南瓜，有梗和发光的脸
- candy(pen, x, y, colour, size=1) 包装糖，(x, y) 是中心；外框约 CANDY_SIZE
- bat(pen, cx, cy, s)              蝙蝠
- dead_tree(pen, x, base, height, seed)  枯树：树干和每根枝都会越往上越细、自然收尖（不会是平头方块）
- tomb(pen, x, base, w, h)         圆顶墓碑，(x, base) 是底座中间

摆位规则：
- 每个会彼此靠近的物件各画成一层，名字用编号（tomb-1、tomb-2、candy-1…），再用
  scene.forbid_overlap_within("tomb-*")、scene.forbid_overlap("candy-*", "tomb-*") 让自检挡住叠放。
- 树干、柱子不要用 rectangle 画；用 dead_tree，或 pen.polygon 画成上窄下宽的梯形。
"""

import math
import random

def pumpkin(pen, cx, cy, r):
    """圆润的南瓜：几瓣叠起来，外瓣深、中间亮，加梗和发光的脸。"""
    lobes = [(-0.62, 0.78, (196, 98, 22)), (0.62, 0.78, (196, 98, 22)),
             (-0.32, 0.92, (226, 122, 30)), (0.32, 0.92, (226, 122, 30)), (0.0, 1.0, (242, 142, 40))]
    for offset, width, colour in lobes:
        x = cx + offset * r
        w = r * width * 0.62
        pen.ellipse([x - w, cy - r * 0.78, x + w, cy + r * 0.62], fill=colour + (255,), outline=(150, 70, 18, 255), width=2)
    # 高光
    pen.ellipse([cx - r * 0.18, cy - r * 0.62, cx + r * 0.08, cy - r * 0.18], fill=(255, 196, 120, 110))
    # 梗
    pen.bezier([(cx - 4, cy - r * 0.72), (cx - 2, cy - r * 1.05), (cx + 16, cy - r * 1.12)], fill=(92, 120, 48, 255), width=9)
    # 脸：三角眼、锯齿嘴，里面亮黄
    glow = (255, 220, 96, 255)
    for side in (-1, 1):
        pen.polygon([(cx + side * r * 0.42, cy - r * 0.30), (cx + side * r * 0.12, cy - r * 0.08),
                     (cx + side * r * 0.46, cy - r * 0.02)], fill=glow)
    mouth = [(cx - r * 0.48, cy + r * 0.12)]
    for k in range(1, 8):
        x = cx - r * 0.48 + k * r * 0.96 / 7
        mouth.append((x, cy + r * (0.20 if k % 2 else 0.12)))
    mouth += [(cx + r * 0.36, cy + r * 0.34), (cx, cy + r * 0.42), (cx - r * 0.36, cy + r * 0.34)]
    pen.polygon(mouth, fill=glow)


def candy(pen, x, y, colour, size=1.0):
    """包装糖：中间椭圆加两边三角，白色描边。(x, y) 是中心。"""
    w, h = 14 * size, 9 * size
    pen.polygon([(x - w, y), (x - w - 11 * size, y - 7 * size), (x - w - 11 * size, y + 7 * size)], fill=colour + (255,))
    pen.polygon([(x + w, y), (x + w + 11 * size, y - 7 * size), (x + w + 11 * size, y + 7 * size)], fill=colour + (255,))
    pen.ellipse([x - w, y - h, x + w, y + h], fill=colour + (255,), outline=(255, 255, 255, 220), width=2)
    pen.line([(x - w * 0.4, y - h * 0.8), (x - w * 0.1, y + h * 0.8)], fill=(255, 255, 255, 150), width=2)


CANDY_SIZE = (54, 22)  # 一颗糖的外框（宽、高）
CANDY_COLOURS = [(238, 92, 124), (120, 198, 238), (244, 204, 72)]


def bat(pen, cx, cy, s):
    pen.polygon([(cx, cy), (cx - s * .55, cy - s * .5), (cx - s * 1.15, cy - s * .15), (cx - s * .8, cy + s * .35),
                 (cx - s * .35, cy + s * .12), (cx, cy + s * .3), (cx + s * .35, cy + s * .12), (cx + s * .8, cy + s * .35),
                 (cx + s * 1.15, cy - s * .15), (cx + s * .55, cy - s * .5)], fill=(150, 128, 190, 255))
    for side in (-1, 1):
        pen.circle((cx + side * s * 0.09, cy - s * 0.05), s * 0.07, fill=(255, 180, 90, 255))


def dead_tree(pen, x, base, height, seed, colour=(56, 44, 58, 255)):
    rng = random.Random(seed)

    def branch(x0, y0, angle, length, width):
        x1, y1 = x0 + length * math.cos(angle), y0 - length * math.sin(angle)
        bend = (x0 + x1) / 2 + rng.uniform(-6, 6), (y0 + y1) / 2 + rng.uniform(-6, 6)
        pen.bezier([(x0, y0), bend, (x1, y1)], fill=colour, width=width)
        if width <= 1.2 or length < 12:
            return
        branch(x1, y1, angle + rng.uniform(0.25, 0.6), length * 0.68, width * 0.72)
        branch(x1, y1, angle - rng.uniform(0.25, 0.6), length * 0.68, width * 0.72)

    branch(x, base, math.pi / 2, height, 13)


def tomb(pen, x, base, w, h):
    pen.pieslice([x - w, base - h * 2, x + w, base], 180, 360, fill=(150, 148, 160, 255), outline=(104, 102, 116, 255), width=3)
    pen.rectangle([x - w, base - h, x + w, base], fill=(150, 148, 160, 255))
    pen.line([(x - w, base - h), (x - w, base)], fill=(104, 102, 116, 255), width=3)
    pen.line([(x + w, base - h), (x + w, base)], fill=(104, 102, 116, 255), width=3)
    pen.rounded_rectangle([x - w - 8, base - 6, x + w + 8, base + 8], radius=3, fill=(96, 88, 108, 255))
    pen.line([(x, base - h * 1.2), (x, base - 22)], fill=(96, 94, 108, 255), width=5)
    pen.line([(x - 16, base - h * 1.2 + 26), (x + 16, base - h * 1.2 + 26)], fill=(96, 94, 108, 255), width=5)
