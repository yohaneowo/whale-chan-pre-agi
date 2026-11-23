"""验收：同一张万圣节图，旧图（她 11/1 发的重画版）对新流程重画，输出对照图与毛病清单。

用法：
  python acceptance.py <她的资料夹（有 whale_sheet.png、whale_cut.png、whale_nohole.png、whale_last.png）> <输出资料夹>

输出：
  compare.png        旧图 | 新图
  compare-zoom.png   同部位放大对照（发箍、脸、手和袖口、尾巴），上排旧、下排新
  acceptance.json    每一项毛病：旧图量到多少、新图量到多少、是否消失、靠自动检查还是要人看
"""

import json
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import halloween  # noqa: E402
from whaledraw.check import character_report  # noqa: E402
from whaledraw.scene import find_font  # noqa: E402
from PIL import ImageFont  # noqa: E402

# 同部位在两张图里的位置（旧图人物位置是她的 fix 脚本决定的，所以分开写）。
ZOOMS = [
    ("发箍、呆毛", (470, 430, 720, 560), (440, 205, 740, 345)),
    ("脸、眼睛", (500, 540, 680, 650), (480, 340, 720, 480)),
    ("手、袖口蕾丝", (440, 660, 560, 740), (395, 555, 500, 625)),
    ("尾巴", (690, 650, 830, 790), (730, 570, 860, 730)),
]


def label(image, text):
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font = ImageFont.truetype(find_font(), 28)
    draw.rectangle([0, 0, 24 + len(text) * 28, 44], fill=(0, 0, 0))
    draw.text((12, 6), text, fill=(255, 255, 255), font=font)
    return out


def zoom_sheet(old, new, tile=300):
    sheet = Image.new("RGB", (tile * len(ZOOMS), tile * 2 + 40), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(find_font(), 22)
    for column, (name, old_box, new_box) in enumerate(ZOOMS):
        for row, (image, box) in enumerate(((old, old_box), (new, new_box))):
            piece = image.crop(box)
            factor = tile / max(piece.size)
            piece = piece.resize((round(piece.width * factor), round(piece.height * factor)), Image.LANCZOS)
            sheet.paste(piece.convert("RGB"), (column * tile, 40 + row * tile))
        draw.text((column * tile + 8, 8), name, fill=(255, 255, 255), font=font)
    return sheet


def main(samples, folder):
    os.makedirs(folder, exist_ok=True)
    final, check = halloween.main(os.path.join(samples, "whale_sheet.png"), folder)
    old = Image.open(os.path.join(samples, "whale_last.png")).convert("RGB")
    new = final.convert("RGB")
    both = Image.new("RGB", (old.width + new.width + 20, max(old.height, new.height)), (0, 0, 0))
    both.paste(label(old, "旧：11/1 重画版"), (0, 0))
    both.paste(label(new, "新：分层＋干净抠图"), (old.width + 20, 0))
    both.save(os.path.join(folder, "compare.png"))
    zoom_sheet(old, new).save(os.path.join(folder, "compare-zoom.png"))

    old_cut = character_report(Image.open(os.path.join(samples, "whale_cut.png")))
    old_patched = character_report(Image.open(os.path.join(samples, "whale_nohole.png")))
    new_cut = check["character"]
    expected = new_cut["expected_hole_pixels"]
    items = [
        {
            "毛病": "人物周围白边",
            "量法": "人物最外圈接近白色的比例（自动）",
            "旧": f"{old_cut['halo']:.1%}（补洞版 {old_patched['halo']:.1%}）",
            "新": f"{new_cut['halo']:.1%}",
            "消失": new_cut["halo"] <= 0.02,
        },
        {
            "毛病": "白边里、眼睛附近的黑点",
            "量法": "人物身上被挖空、会露出背景的像素，加上零碎残渣块数（自动）",
            "旧": f"挖空 {old_cut['hole_pixels']} 像素、残渣 {old_cut['specks']} 块（补洞版挖空 {old_patched['hole_pixels']}）",
            "新": f"非故意挖空 {max(0, new_cut['hole_pixels'] - expected)} 像素、残渣 {new_cut['specks']} 块"
                  f"（故意挖掉发丝间等空隙约 {expected} 像素）",
            "消失": new_cut["specks"] == 0 and new_cut["hole_pixels"] <= expected * 1.15 + 20,
        },
        {
            "毛病": "尾巴旁方块色差／直线断面",
            "量法": "人物被裁切框切到的像素（自动）；整张图从各层重新合成、不在成品上补丁（流程保证）；看 compare-zoom.png「尾巴」（人看）",
            "旧": f"裁切框切到 {old_cut['cut_edge_pixels']} 像素（尾巴右侧、脚下被切平），成品上有补丁",
            "新": f"裁切框切到 {new_cut['cut_edge_pixels']} 像素，没有补丁",
            "消失": new_cut["cut_edge_pixels"] == 0,
        },
        {
            "毛病": "糖果压到人物",
            "量法": "candy 层与 character 层的不透明交集（自动，压到就不出图）",
            "旧": "旧图没有分层，量不到；见 compare.png（人看）",
            "新": f"{sum(o['pixels'] for o in check['overlaps'] if o['top'] == 'candy')} 像素",
            "消失": not any(o["top"] == "candy" for o in check["overlaps"]),
        },
    ]
    result = {
        "items": items,
        "self_check_problems": check["problems"],
        "still_needs_eyes": [
            "抠图时被围住的白色空隙哪些是背景（发丝间、呆毛圈）——程式分不出它和眼睛高光、围裙，第一次抠图要看 enclosed.png 决定，之后重复用",
            "整体好不好看、风格统不统一、调色",
            "南瓜、糖果这类零件画得像不像",
        ],
    }
    with open(os.path.join(folder, "acceptance.json"), "w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return result


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
