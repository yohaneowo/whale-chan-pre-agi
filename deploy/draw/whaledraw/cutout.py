"""人物抠图：白底（或任何纯色底）的立绘 → 边缘干净的透明 PNG。只做一次，存起来当固定底图。

为什么不是「接近白色就透明」：那样会把人物身上的白色（围裙、发箍、袜子）一起挖空，边缘那圈半白的混色
像素又留成不透明，贴到深色背景上就是一圈参差白边。这里改成：

1. 只有「从图边连通」的底色才算背景（围裙、发箍这种被线稿围住的白色不会被挖）。
2. 被线稿围住、却其实是底色的小空隙（呆毛圈里、鞋带孔、发丝之间、两腿之间）找出来列在报告里，
   用 `background_seeds` 指定要挖哪几块。这些空隙和人物身上的白色（围裙、眼睛高光）像素上分不出来，
   所以不自动挖：没指定的一律保留，宁可留白不挖洞。
3. 边缘一圈做「反混色」：按像素离底色多远算透明度，把底色成分从颜色里扣掉，半白的边缘就变成
   半透明的线稿色，不会再发白。
4. 跟主体不相连的零碎小点（抠图残渣，贴上去就是黑点）丢掉。

只收不透明的输入（纯色底）；已经有透明部分的图会直接报错，不会默默处理。
只用 Pillow、numpy、scipy。
"""

from dataclasses import dataclass, field

import numpy as np
from PIL import Image
from scipy import ndimage


@dataclass
class Cutout:
    image: Image.Image  # RGBA，边缘已反混色
    report: dict = field(default_factory=dict)

    @property
    def alpha(self):
        return np.asarray(self.image.getchannel("A"))


def _background_colour(rgb):
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    return np.median(border, axis=0)


def _distance(rgb, colour):
    return np.abs(rgb - colour).max(axis=2)


def cut_out(
    source,
    crop=None,
    tolerance=24,
    band=2,
    shadow_box=None,
    background_seeds=(),
    enclosed_purity=6,
    enclosed_min_area=12,
    speck_max_area=40,
    remove_ids=(),
):
    """把 source（路径或 Image）抠成透明底。

    crop: (左, 上, 右, 下)，先裁出要的那个人物。
    tolerance: 和底色差多少以内算底色（0–255，看最大的那个通道差）。
    band: 边缘反混色的宽度（像素）。
    shadow_box: (左, 上, 右, 下)，这个框里跟背景连着的灰色地面阴影也当背景（低彩度、不太暗的）。
    background_seeds: [(x, y), ...]，这些点所在的被围住空隙挖掉（座标以裁切后为准；看 report["enclosed"]
        或 check.enclosed_sheet 的编号图决定）。没指定的一律保留。
    remove_ids: 另一种指定法，直接用编号图上的编号（同一张原图、同一个裁切框，编号每次都一样）。
        候选零件工具和维护员的封印工具都用编号，所以两边算出来的结果会一模一样。

    只收「不透明」的图（纯色底的立绘、截图）。已经有透明部分的图（PNG 去背过的）会报 ValueError，
    不会默默把透明处当成黑色或白色再抠：那种图本来就不用抠，直接用；要重抠就先自己铺上底色。
    """
    image = Image.open(source) if isinstance(source, str) else source
    if image.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in image.info:
        alpha = image.convert("RGBA").getchannel("A")
        if alpha.getextrema()[0] < 255:
            raise ValueError(
                "cut_out 只收不透明的图：这张已经有透明部分（最低透明度 "
                f"{alpha.getextrema()[0]}）。已经去背的图直接用；要重抠请先铺上纯色底。"
            )
    image = image.convert("RGB")
    if crop:
        image = image.crop(crop)
    rgb = np.asarray(image).astype(np.float32)
    height, width = rgb.shape[:2]
    bg = _background_colour(rgb)
    distance = _distance(rgb, bg)

    # 1. 从图边连通的底色
    candidate = distance <= tolerance
    if shadow_box:
        x0, y0, x1, y1 = shadow_box
        chroma = rgb.max(axis=2) - rgb.min(axis=2)
        shade = (chroma <= 32) & (rgb.min(axis=2) >= 100)
        box = np.zeros_like(candidate)
        box[max(0, y0):y1, max(0, x0):x1] = True
        candidate = candidate | (shade & box)
    labels, count = ndimage.label(candidate)
    edge_labels = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    edge_labels.discard(0)
    background = np.isin(labels, list(edge_labels))
    edge_background = int(background.sum())

    # 2. 被围住的底色空隙：发丝之间、呆毛圈里、鞋带孔、两腿之间。它们和人物身上的白色（围裙、发箍、
    #    眼睛高光）在像素上几乎一样，程式分不准，所以只列出来，靠 background_seeds 指定要挖哪几块；
    #    一个人物只要看一次（check.enclosed_sheet 画编号图），之后重复用同一份抠图。
    pure = (distance <= enclosed_purity) & ~background
    hole_labels, _ = ndimage.label(pure)
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    seeds = [(int(x), int(y)) for x, y in background_seeds]
    seed_labels = {}
    for x, y in seeds:
        if not (0 <= x < width and 0 <= y < height):
            continue
        # 点不一定正好落在纯底色上：在半径 6 像素内找最近的空隙。
        y0, y1, x0, x1 = max(0, y - 6), min(height, y + 7), max(0, x - 6), min(width, x + 7)
        window = hole_labels[y0:y1, x0:x1]
        if window.any():
            ys, xs = np.nonzero(window)
            nearest = np.argmin((ys + y0 - y) ** 2 + (xs + x0 - x) ** 2)
            seed_labels[(x, y)] = int(window[ys[nearest], xs[nearest]])
    chosen = set(seed_labels.values())
    wanted_ids = {int(i) for i in remove_ids}
    holes = []
    for index, region in enumerate(ndimage.find_objects(hole_labels), start=1):
        if region is None:
            continue
        mask = hole_labels[region] == index
        area = int(mask.sum())
        if area < enclosed_min_area:
            continue
        if len(holes) in wanted_ids:
            chosen.add(index)
        loose_label = labels[region][mask][0]
        grow = labels == loose_label if loose_label else hole_labels == index
        # 提示用：空隙外圈有几成是鲜艳的颜色。发丝间的空隙外圈多半是头发色，但眼睛高光也一样，只当参考。
        ring = ndimage.binary_dilation(grow, iterations=4) & ~ndimage.binary_dilation(grow, iterations=1)
        ring &= distance > tolerance
        ring_colour = float((chroma[ring] > 40).mean()) if ring.any() else 0.0
        cy, cx = ndimage.center_of_mass(mask)
        remove = index in chosen
        holes.append({
            "id": len(holes),
            "center": [int(region[1].start + cx), int(region[0].start + cy)],
            "bbox": [int(region[1].start), int(region[0].start), int(region[1].stop), int(region[0].stop)],
            "area": area,
            "ring_colour": round(ring_colour, 2),
            "removed": bool(remove),
        })
        if remove:
            # 挖掉整块宽松底色（含它边上那圈不那么纯的像素），之后边缘反混色会处理过渡。
            background |= grow
    unmatched = [[x, y] for x, y in seeds if (x, y) not in seed_labels]
    unknown_ids = sorted(wanted_ids - {hole["id"] for hole in holes})
    removed_pixels = int(background.sum() - edge_background)

    # 3. 透明度：背景 0，其余 1；边缘一圈按离底色的距离反混色。
    alpha = np.where(background, 0.0, 1.0).astype(np.float32)
    near = ndimage.binary_dilation(background, iterations=band) & ~background
    scale = np.maximum(np.maximum(bg, 255 - bg), 1)
    edge_alpha = (np.abs(rgb - bg) / scale).max(axis=2)
    edge_alpha = np.clip(edge_alpha * 1.15, 0, 1)
    alpha[near] = edge_alpha[near]
    alpha[alpha < 0.06] = 0
    safe = np.maximum(alpha, 1e-3)[..., None]
    colour = np.where(
        near[..., None],
        np.clip((rgb - (1 - safe) * bg) / safe, 0, 255),
        rgb,
    )

    # 4. 丢掉跟主体不相连的小残渣
    solid = alpha > 0
    parts, part_count = ndimage.label(solid, structure=np.ones((3, 3)))
    sizes = ndimage.sum(solid, parts, range(1, part_count + 1)) if part_count else []
    dropped = 0
    for index, size in enumerate(sizes, start=1):
        if size <= speck_max_area:
            alpha[parts == index] = 0
            dropped += 1

    out = np.dstack([colour, alpha * 255]).round().astype(np.uint8)
    out[alpha == 0, :3] = 0
    result = Image.fromarray(out, "RGBA")
    bbox = result.getbbox()
    report = {
        "size": [width, height],
        "background_colour": [int(round(c)) for c in bg],
        "content_bbox": list(bbox) if bbox else None,
        "enclosed": holes,
        "unmatched_seeds": unmatched,
        "unknown_remove_ids": unknown_ids,
        # 用 background_seeds 故意挖掉的像素数：自检时人物身上的洞超过这个数才算问题。
        "removed_pixels": removed_pixels,
        "specks_dropped": dropped,
    }
    return Cutout(result, report)


def cut_edges(image, threshold=128):
    """每一边有几个不透明像素直接碰到图的边（被裁切框切到，贴上去会出现直线断面）。
    回传 {"top": n, "bottom": n, "left": n, "right": n}；完整的人物四边都是 0。"""
    alpha = np.asarray(image.convert("RGBA").getchannel("A")) > threshold
    return {"top": int(alpha[0].sum()), "bottom": int(alpha[-1].sum()),
            "left": int(alpha[:, 0].sum()), "right": int(alpha[:, -1].sum())}


def edge_quality(image, white_min=200, chroma_max=24):
    """量一张透明底人物图的边缘问题，新旧抠图都能量。

    halo: 最外圈（不透明、旁边就是透明）的像素里，接近白色的比例。贴到深色背景上就是白边。
    specks: 跟最大一块主体不相连的零碎小块数（贴上去是黑点或白点）。
    holes: 被人物围住、但完全透明的洞的总面积（围裙、发箍被挖空就会在这里）。
    """
    rgba = np.asarray(image.convert("RGBA")).astype(np.float32)
    alpha = rgba[..., 3] / 255
    solid = alpha > 0.5
    clear = alpha < 0.05
    outer = solid & ndimage.binary_dilation(clear, structure=np.ones((3, 3)))
    rgb = rgba[..., :3]
    whitish = (rgb.min(axis=2) >= white_min) & ((rgb.max(axis=2) - rgb.min(axis=2)) <= chroma_max)
    halo = float((outer & whitish).sum() / max(outer.sum(), 1))
    parts, count = ndimage.label(alpha > 0.05, structure=np.ones((3, 3)))
    if count:
        sizes = ndimage.sum(np.ones_like(alpha), parts, range(1, count + 1))
        specks = int(sum(1 for s in sizes if s < max(sizes)))
    else:
        specks = 0
    transparent, tcount = ndimage.label(clear)
    edge = set(np.unique(np.concatenate([transparent[0], transparent[-1], transparent[:, 0], transparent[:, -1]])))
    sizes = np.bincount(transparent.ravel())
    holes = int(sum(sizes[i] for i in range(1, tcount + 1) if i not in edge))
    return {"halo": round(halo, 4), "outer_pixels": int(outer.sum()), "specks": specks, "hole_pixels": holes}
