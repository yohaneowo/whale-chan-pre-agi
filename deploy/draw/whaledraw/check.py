"""出图前自检：自动量得到的就自动报，量不到的做成一眼能看的检查图（给她用 read_image 看，或给站长看）。

自动：
- 人物边缘白边比例、人物身上的洞（贴上去就是黑点）、零碎残渣、人物被裁切框切到的边（直线断面）。
- 不准重叠的层有没有压到（例如糖果压人物）。
要人看（做成图）：
- check-grid.png：成品加 100 像素刻度线，方便说「把南瓜往左移 50」。
- check-edges.png：人物轮廓上几个位置放大四倍（贴在深色、浅色两种底上），看白边、发丝、蕾丝。
- check-overlap.png：各层的范围上色，红色是压到的地方。
- enclosed_sheet()：抠图时被围住的白色空隙编号图，决定哪几块是背景要挖。
"""

import json
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

from .cutout import cut_edges, edge_quality
from .scene import find_font


class CheckFailed(Exception):
    """自检没过：成品不输出，只留一张标了「未通过」的图在 rejected 路径。"""

    def __init__(self, problems, rejected, report):
        super().__init__("自检没过，没有输出成品：" + "；".join(problems) + f"（看 {rejected}）")
        self.problems, self.rejected, self.report = problems, rejected, report


def straight_cuts(image, min_run=12):
    """人物图被裁切框切到的地方：不透明像素直接碰到图的边。回传碰到的像素数（0 才是完整人物）。"""
    return sum(cut_edges(image).values())


# 不是封印零件的图片层，看得见的范围超过画面这个比例就算问题：大张的人物或候选零件不能绕过封印直接贴。
MAX_LOOSE_IMAGE_FRACTION = 0.10


def character_report(image):
    report = edge_quality(image)
    report["cut_edge_pixels"] = straight_cuts(image)
    return report


def _font(size):
    try:
        return ImageFont.truetype(find_font(), size)
    except (OSError, FileNotFoundError):
        return ImageFont.load_default()


def grid(image, step=100):
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out, "RGBA")
    font = _font(14)
    for x in range(0, out.width, step):
        draw.line([(x, 0), (x, out.height)], fill=(255, 255, 255, 70), width=1)
        draw.text((x + 2, 2), str(x), fill=(255, 255, 0, 230), font=font)
    for y in range(0, out.height, step):
        draw.line([(0, y), (out.width, y)], fill=(255, 255, 255, 70), width=1)
        draw.text((2, y + 2), str(y), fill=(255, 255, 0, 230), font=font)
    return out


def edge_points(alpha, count=6):
    """轮廓上平均挑几个点：最上、最左、最右、左下、右下，再加呆毛／头顶附近。"""
    solid = alpha > 128
    ys, xs = np.nonzero(solid)
    if not len(xs):
        return []
    picks = [
        (xs[np.argmin(ys)], ys.min()),
        (xs.min(), ys[np.argmin(xs)]),
        (xs.max(), ys[np.argmax(xs)]),
    ]
    low = ys > ys.min() + (ys.max() - ys.min()) * 0.75
    picks.append((xs[low].min(), ys[low][np.argmin(xs[low])]))
    picks.append((xs[low].max(), ys[low][np.argmax(xs[low])]))
    mid = (ys > ys.min() + (ys.max() - ys.min()) * 0.3) & (ys < ys.min() + (ys.max() - ys.min()) * 0.5)
    if mid.any():
        picks.append((xs[mid].min(), ys[mid][np.argmin(xs[mid])]))
    return [(int(x), int(y)) for x, y in picks[:count]]


def edge_sheet(layer, points, size=56, zoom=4, backgrounds=((34, 24, 60), (235, 230, 220))):
    """人物层在几个点放大：每个点贴在深色和浅色两种底上，白边、黑边都看得出来。"""
    tile = size * zoom
    sheet = Image.new("RGB", (tile * len(points), tile * len(backgrounds)), (0, 0, 0))
    for column, (x, y) in enumerate(points):
        box = (x - size // 2, y - size // 2, x + size // 2, y + size // 2)
        piece = layer.crop(box)
        for row, colour in enumerate(backgrounds):
            base = Image.new("RGBA", piece.size, colour + (255,))
            base.alpha_composite(piece)
            sheet.paste(base.convert("RGB").resize((tile, tile), Image.NEAREST), (column * tile, row * tile))
    return sheet


def overlap_sheet(scene, names, unruled=()):
    out = Image.new("RGB", (scene.width, scene.height), (20, 20, 20))
    colours = [(70, 110, 230), (80, 200, 120), (230, 180, 60), (180, 90, 220), (90, 200, 220)]
    pixels = np.asarray(out).copy()
    for index, name in enumerate(names):
        pixels[scene.mask(name)] = colours[index % len(colours)]
    for item in unruled:                      # 橙色：重叠了、却没设规则（提醒，不是错误）
        pixels[scene.mask(item["top"]) & scene.mask(item["bottom"])] = (255, 150, 0)
    for top, bottom, margin in scene._pairs():
        pixels[scene.mask(top) & scene.mask(bottom)] = (255, 30, 30)
    out = Image.fromarray(pixels)
    draw = ImageDraw.Draw(out)
    font = _font(18)
    for index, name in enumerate(names):
        draw.text((10, 10 + index * 24), name, fill=colours[index % len(colours)], font=font)
    return out


def enclosed_sheet(cutout, zoom=2):
    """抠图后被围住的白色空隙编号图：绿框已挖成透明，红框保留。看图决定 background_seeds。"""
    image = cutout.image
    base = Image.new("RGBA", image.size, (60, 50, 90, 255))
    base.alpha_composite(image)
    out = base.convert("RGB").resize((image.width * zoom, image.height * zoom), Image.NEAREST)
    draw = ImageDraw.Draw(out)
    font = _font(12 * zoom // 2 + 8)
    for hole in cutout.report.get("enclosed", []):
        x0, y0, x1, y1 = (v * zoom for v in hole["bbox"])
        colour = (60, 220, 90) if hole["removed"] else (240, 60, 60)
        draw.rectangle([x0, y0, x1, y1], outline=colour, width=2)
        draw.text((x0, y0 - 16), str(hole["id"]), fill=colour, font=font)
    return out


def self_check(scene, folder, character="character", rules_layers=None):
    """出图前跑一次：写检查图与 check.json，回传问题清单（空的才算过）。"""
    os.makedirs(folder, exist_ok=True)
    final = scene.render(check=False)
    grid(final).save(os.path.join(folder, "check-grid.png"))
    layer = scene.layer_image(character)
    alpha = np.asarray(layer.getchannel("A"))
    points = edge_points(alpha)
    edge_sheet(layer, points).save(os.path.join(folder, "check-edges.png"))
    review = scene.unruled_overlaps([character])
    names = rules_layers or sorted({n for pair in scene._pairs() for n in pair[:2]}
                                   | {n for item in review["unruled"] for n in (item["top"], item["bottom"])})
    overlap_sheet(scene, names, review["unruled"]).save(os.path.join(folder, "check-overlap.png"))
    source = scene.layers[character].source
    quality = character_report(source)
    overlaps = scene.overlaps()
    problems = []
    if quality["halo"] > 0.02:
        problems.append(f"人物边缘有白边（最外圈 {quality['halo']:.1%} 是白的）")
    layer_info = scene.layers[character]
    expected = layer_info.expected_holes
    quality["expected_hole_pixels"] = round(expected)
    quality["part"] = layer_info.part
    quality["expected_from"] = "override" if layer_info.holes_override else ("seal" if layer_info.part else "none")
    overrides = []
    if layer_info.part is None:
        problems.append("人物不是用 load_part() 读的封印零件，没办法确认哪些洞是本来就有的")
    if layer_info.holes_override:
        value, reason = layer_info.holes_override
        overrides.append({"layer": character, "holes_override": value, "reason": reason})
        problems.append(f"人物的预期洞数被手动改成 {value}（理由：{reason}）。这会让「挖空」检查失效，要站长确认才能发")
    if quality["hole_pixels"] > expected * 1.15 + 20:
        problems.append(f"人物身上有 {quality['hole_pixels'] - round(expected)} 像素被挖空（贴上去会露出背景，像黑点）")
    if quality["specks"]:
        problems.append(f"人物旁边有 {quality['specks']} 块零碎残渣")
    # 裁切边用贴上去之前量的（scene.image 会补透明边）；封印写明故意裁掉的边（半身特写）不算。
    sides = layer_info.cut_sides or cut_edges(source)
    quality["cut_edges"] = sides
    quality["cut_edges_ok"] = list(layer_info.cut_edges_ok)
    bad_sides = {side: n for side, n in sides.items() if n and side not in layer_info.cut_edges_ok}
    quality["cut_edge_pixels"] = sum(bad_sides.values())
    if bad_sides:
        problems.append(f"人物被裁切框切到（{', '.join(f'{s} {n} 像素' for s, n in bad_sides.items())}），会出现直线断面")
    canvas = scene.width * scene.height
    for name, layer in scene.layers.items():
        if layer.source is None or layer.part is not None:
            continue
        visible = int(scene.mask(name).sum())
        if visible > canvas * MAX_LOOSE_IMAGE_FRACTION:
            problems.append(f"图片层 {name} 不是封印零件，却占了画面 {visible / canvas:.0%}："
                            "人物与大张的图要用 load_part() 读封印零件（候选零件要等维护员封印）")
    for item in overlaps:
        problems.append(f"{item['top']} 压到 {item['bottom']}：{item['pixels']} 像素，范围 {item['bbox']}")
    rule_problems = scene.rule_problems()
    problems += [item["message"] for item in rule_problems]
    # 提醒（warnings）：不挡出图，但 check.json 会写、check-overlap.png 上用橙色标出来。
    warnings = [
        f"「{item['other']}」跟人物「{item['character']}」重叠了 {item['pixels']} 像素（范围 {item['bbox']}，"
        f"{item['top']} 压在 {item['bottom']} 上面），可是没有为这一对设规则。不准叠就用 "
        f"scene.forbid_overlap({item['top']!r}, {item['bottom']!r})；故意叠就用 "
        f"scene.allow_overlap({item['character']!r}, {item['other']!r}, \"理由\")"
        for item in review["unruled"]
    ] + scene.allowance_warnings()
    report = {"character": quality, "overlaps": overlaps, "edge_points": points, "problems": problems,
              "warnings": warnings}
    if review["unruled"]:
        report["unruled_overlaps"] = review["unruled"]
    if review["allowed"]:
        report["allowed_overlaps"] = review["allowed"]     # 她声明的「故意重叠」：审阅时看理由合不合理
    if review["skipped_large"]:
        report["skipped_large_layers"] = review["skipped_large"]  # 太大的场景层没有检查重叠，审阅时看一眼
    if rule_problems:
        report["unchecked_rules"] = rule_problems  # 标红：没对到图层、或太大而没检查的规则
    if overrides:
        report["overrides"] = overrides  # 标红：手动覆盖过的检查
    with open(os.path.join(folder, "check.json"), "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=1)
    return report


def summarize(report):
    """入口（halloween.py 之类、她自己的 draw.py）出图后要印出来的自检重点：problems 和 warnings 一律有；
    有内容才多印 allowed_overlaps（她声明的故意重叠与理由）和 skipped_large_layers（没检查重叠的大场景层）。
    成功出图时也要印：提醒不挡出图，不印就看不到。"""
    out = {"problems": report["problems"], "warnings": report.get("warnings", [])}
    for key in ("allowed_overlaps", "skipped_large_layers"):
        if report.get(key):
            out[key] = report[key]
    return out


def _stamp_rejected(image, problems):
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out, "RGBA")
    font = _font(30)
    small = _font(18)
    draw.rectangle([0, 0, out.width, 48 + 24 * min(len(problems), 4)], fill=(170, 0, 0, 220))
    draw.text((14, 8), "未通过自检，不是成品", fill=(255, 255, 255), font=font)
    for index, problem in enumerate(problems[:4]):
        draw.text((14, 48 + index * 24), "・" + problem[:70], fill=(255, 235, 235), font=small)
    return out


def publish(scene, path, check_folder, character="character"):
    """出图的唯一出口：先自检，过了才写 path；没过就删掉 path 上的旧成品，把图盖上「未通过」存到
    <名字>.REJECTED.png，然后报 CheckFailed。这样她不会把没过关的图当成品发出去。"""
    report = self_check(scene, check_folder, character)
    final = scene.render(check=False)
    root, ext = os.path.splitext(path)
    rejected = root + ".REJECTED" + (ext or ".png")
    if report["problems"]:
        if os.path.exists(path):
            os.remove(path)
        _stamp_rejected(final, report["problems"]).save(rejected)
        raise CheckFailed(report["problems"], rejected, report)
    if os.path.exists(rejected):
        os.remove(rejected)
    final.convert("RGB").save(path)
    return final, report
