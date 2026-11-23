"""万圣节图（2026-11-01「尖叫的万圣节」）用新流程重画：同样的构图，分层、抗锯齿、干净抠图、不压人物。

用法：
  python halloween.py cut <whale_sheet.png> <whale_front.png>   抠正面立绘（只做一次），交给维护员封印
  python halloween.py <封印过的零件.png> <输出资料夹>          画图，例如 /opt/dsh/whaledraw-0.2.0/parts/whale_front.png

whale_sheet.png 是她的人物设定图（白底，左上是正面）。抠出来的 png 要由维护员用 seal_part.py 封印、放进
安装目录的 parts/，load_part() 才读得进来。
改哪个东西就改哪一层的函数，再整张重跑；不要在 halloween.png 上改像素。
"""

import json
import math
import os
import random
import sys

from PIL import Image
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from whaledraw.check import CheckFailed, character_report, enclosed_sheet, publish, summarize  # noqa: E402
from whaledraw.cutout import cut_out  # noqa: E402
from whaledraw.parts import load_part  # noqa: E402
from whaledraw.props import CANDY_COLOURS, CANDY_SIZE, bat, candy, dead_tree, pumpkin, tomb  # noqa: E402
from whaledraw.scene import Scene  # noqa: E402

W, H = 1200, 800
HORIZON = 540

# 正面立绘在设定图里的位置，以及哪些被围住的白色空隙其实是背景（看 enclosed.png 的编号图决定的）：
# 呆毛圈里、左右发丝间、尾巴旁发丝间、两腿之间、两个鞋带孔。眼睛高光、围裙、发箍、袖口都保留。
FRONT = {
    "crop": (0, 0, 548, 624),
    "shadow_box": (0, 585, 548, 624),
    "background_seeds": [
        (216, 43), (443, 325), (83, 329), (482, 401), (30, 405),
        (440, 479), (445, 498), (134, 541), (254, 573), (303, 581), (206, 581),
    ],
}


def cut_front(sheet_path, png_path):
    """抠正面立绘，只做一次：存成 png（旁边放 enclosed.png 编号图）。之后要维护员封印才能用。"""
    cut = cut_out(sheet_path, **FRONT)
    folder = os.path.dirname(os.path.abspath(png_path))
    os.makedirs(folder, exist_ok=True)
    cut.image.save(png_path)
    enclosed_sheet(cut).save(os.path.join(folder, "enclosed.png"))
    return cut


# ---------- 场景 ----------

def build(character, font_path=None):
    """character：load_part() 读进来的封印零件。"""
    scene = Scene(W, H, font_path=font_path)
    feet = (600, 772)
    # 人物先放，后面的道具都按她的位置摆。夜景往紫色拉一点，不会像贴纸一样亮。
    scene.image("character", 20, character, feet=feet, height=560, tint=((150, 136, 210), 0.22))
    head = scene.anchor("character", "top")
    # 气泡尾巴从气泡底往她的头指，碰到她之前 12 像素就停（最长 70），不会压到人物。
    near_her = ndimage.binary_dilation(scene.mask("character"), iterations=12)
    start = (259, 207)
    bubble_tip = start
    for step in range(1, 71):
        length = math.dist(start, head) or 1
        x = start[0] + (head[0] - start[0]) * step / length
        y = start[1] + (head[1] - start[1]) * step / length
        if near_her[min(H - 1, max(0, round(y))), min(W - 1, max(0, round(x)))]:
            break
        bubble_tip = (x, y)
    pumpkins = [(220, 690, 78), (985, 700, 70)]

    @scene.vector("sky", z=0)
    def sky(pen):
        for y in range(0, HORIZON + 2, 2):
            t = y / HORIZON
            pen.rectangle([0, y, W, y + 2], fill=(int(28 + 22 * t), int(18 + 10 * t), int(64 + 30 * t), 255))
        rng = random.Random(7)
        for _ in range(150):
            x, y, r = rng.uniform(0, W), rng.uniform(0, 470), rng.choice([0.8, 0.8, 1.2, 1.8])
            pen.circle((x, y), r, fill=(255, 255, 235, rng.randint(90, 220)))

    @scene.vector("moon", z=1)
    def moon(pen):
        cx, cy = 1000, 120
        for k in range(9):
            rr = 52 + k * 13
            pen.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=(255, 214, 140, 150 - k * 14), width=3)
        pen.ellipse([cx - 46, cy - 46, cx + 30, cy + 46], fill=(255, 243, 206, 255))
        pen.ellipse([cx - 30, cy - 46, cx + 46, cy + 46], fill=(36, 24, 72, 255))

    @scene.vector("bats", z=2)
    def bats(pen):
        for x, y, s in [(590, 88, 30), (700, 66, 22), (470, 118, 18), (880, 142, 20), (960, 88, 16)]:
            bat(pen, x, y, s)

    @scene.vector("trees", z=3)
    def trees(pen):
        dead_tree(pen, 120, HORIZON + 10, 150, seed=3)
        dead_tree(pen, 1080, HORIZON + 10, 140, seed=5)

    @scene.vector("ground", z=4)
    def ground(pen):
        pen.rectangle([0, HORIZON, W, H], fill=(44, 34, 54, 255))
        pen.line([(0, HORIZON), (W, HORIZON)], fill=(88, 70, 104, 255), width=4)
        rng = random.Random(11)
        for _ in range(90):
            x, y, r = rng.uniform(0, W), rng.uniform(560, H - 10), rng.uniform(5, 16)
            pen.ellipse([x, y, x + r, y + r / 2], fill=(64, 50, 64, 120))
        for _ in range(700):
            x, y, h = rng.uniform(0, W), rng.uniform(548, H - 4), rng.uniform(6, 16)
            pen.line([(x, y), (x + rng.uniform(-4, 4), y - h)],
                     fill=(70 + rng.randint(0, 40), 96 + rng.randint(0, 40), 66, 210), width=1.6)

    # 每块墓碑各一层，自检才挡得住墓碑彼此叠放、糖果压在墓碑上。
    for number, (x, base, w, h) in enumerate([(150, 560, 50, 64), (300, 530, 34, 44), (1075, 562, 46, 60)], start=1):
        scene.vector(f"tomb-{number}", z=5)(lambda pen, x=x, base=base, w=w, h=h: tomb(pen, x, base, w, h))

    @scene.vector("shadows", z=6, blur=4)
    def shadows(pen):
        x, y = feet
        pen.ellipse([x - 150, y - 16, x + 150, y + 14], fill=(10, 6, 20, 150))
        for px, py, r in pumpkins:
            pen.ellipse([px - r * 1.1, py + r * 0.48, px + r * 1.1, py + r * 0.78], fill=(10, 6, 20, 140))

    @scene.vector("pumpkins", z=7)
    def draw_pumpkins(pen):
        for px, py, r in pumpkins:
            pumpkin(pen, px, py, r)

    # 糖果：南瓜顶上各三颗，地上再撒几颗；位置用 free_spot 找，不压人物、不压南瓜、彼此不叠。
    taken = []
    spots = []
    for px, py, r in pumpkins:
        for k, colour in enumerate(CANDY_COLOURS):
            near = (px + (k - 1) * 50, py - r * 0.78 - 12)
            x, y = scene.free_spot(CANDY_SIZE, near, avoid=["character", "pumpkins", "tomb-1", "tomb-2", "tomb-3"], margin=1, taken=taken)
            taken.append((x, y, x + CANDY_SIZE[0], y + CANDY_SIZE[1]))
            spots.append((x + CANDY_SIZE[0] / 2, y + CANDY_SIZE[1] / 2, colour))
    for k, near in enumerate([(420, 760), (780, 768), (340, 610)]):
        x, y = scene.free_spot(CANDY_SIZE, near, avoid=["character", "pumpkins", "tomb-1", "tomb-2", "tomb-3"], margin=6, taken=taken,
                               within=(0, HORIZON + 30, W, H))
        taken.append((x, y, x + CANDY_SIZE[0], y + CANDY_SIZE[1]))
        spots.append((x + CANDY_SIZE[0] / 2, y + CANDY_SIZE[1] / 2, CANDY_COLOURS[k % 3]))

    @scene.vector("candy", z=21)
    def candies(pen):
        for x, y, colour in spots:
            candy(pen, x, y, colour)

    @scene.vector("scream", z=22)
    def scream(pen):
        cx, cy = head[0], head[1] + 150
        for k in range(12):
            a = math.pi * (0.15 + k * 0.064)
            r1, r2 = 205, 250
            pen.line([(cx + r1 * math.cos(a), cy - r1 * math.sin(a)), (cx + r2 * math.cos(a), cy - r2 * math.sin(a))],
                     fill=(255, 236, 150, 200), width=5)

    @scene.vector("bubble", z=23)
    def bubble(pen):
        pen.rounded_rectangle([60, 60, 430, 210], radius=30, fill=(255, 255, 255, 242), outline=(255, 140, 60, 255), width=5)
        tip = bubble_tip
        pen.polygon([(230, 205), (288, 205), tip], fill=(255, 255, 255, 242))
        pen.line([(233, 207), tip, (286, 207)], fill=(255, 140, 60, 255), width=5)
        pen.text((245, 110), "尖叫的萬聖節", 40, (226, 96, 50, 255))
        pen.text((245, 170), "糖果交出來！", 34, (214, 140, 90, 255))

    @scene.vector("signature", z=24)
    def signature(pen):
        pen.text((1150, 772), "11/1 重畫版", 26, (190, 170, 220, 255), anchor="rm")

    scene.forbid_overlap("candy", "character")
    scene.forbid_overlap("candy", "pumpkins")
    scene.forbid_overlap("candy", "tomb-*")
    scene.forbid_overlap_within("tomb-*")
    scene.forbid_overlap("pumpkins", "tomb-*")
    scene.forbid_overlap("bubble", "character")
    scene.forbid_overlap("signature", "character")
    scene.allow_overlap("character", "shadows", "影子本来就在她脚下，人物压在影子上")
    return scene


def render_to(character, folder):
    """画一张、自检、过了才写 folder/halloween.png；没过会报 CheckFailed（见 whaledraw.check.publish）。"""
    scene = build(character)
    scene.save_layers(os.path.join(folder, "layers"))
    final, check = publish(scene, os.path.join(folder, "halloween.png"), os.path.join(folder, "check"))
    return final, check


def main(part_png, folder):
    character = load_part(part_png)
    try:
        final, check = render_to(character, folder)
    except CheckFailed as error:
        print(json.dumps({"ok": False, **summarize(error.report), "rejected": error.rejected}, ensure_ascii=False))
        raise
    # 成功出图也要印 warnings（以及有的话 allowed_overlaps、skipped_large_layers）：提醒不挡出图，不印就看不到。
    print(json.dumps({"ok": True, **summarize(check), "character": check["character"],
                      "cutout": character_report(character.image)}, ensure_ascii=False))
    return final, check


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "cut":
        cut_front(sys.argv[2], sys.argv[3])
        print(f"抠好了：{sys.argv[3]}。请维护员用 seal_part.py 封印后放进 parts/ 再用")
    else:
        main(sys.argv[1], sys.argv[2])
