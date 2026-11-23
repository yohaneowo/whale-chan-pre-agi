"""分层合成：背景、人物、道具、文字各自一张透明图，按顺序叠起来。

- 每一层都是一个函数，每次从头画，不在合成后的整张图上改像素：改树就只改树那一层的函数。
- 形状层用四倍大小画、再用 LANCZOS 缩小，边缘自然平滑（Pillow 本身画圆、斜线没有抗锯齿）。
- 函数里的座标一律写最终尺寸（例如 1200×800），Pen 自己乘四。
- 摆位用 anchor（人物脚边、头顶……）和 free_spot（附近找一块不压到人物的空位），不用猜座标。
- 合成前检查重叠：forbid_overlap("candy", "character") 之后，糖果压到人物就报错，不会默默出图。
"""

import fnmatch
import os

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from scipy import ndimage

from .cutout import cut_edges
from .parts import Part, verify

# 一条规则的一边最多对到几层。forbid_overlap_within 是两两比较，64 层是 2016 对；再多就当问题，不展开。
MAX_RULE_LAYERS = 64
# 「没设规则的重叠」提醒不看大的场景层（天空、地面、光芒这类）：看得见的范围超过画面这个比例就略过，
# 这些层本来就会在人物后面或脚下。略过的层名会写进 check.json 的 skipped_large_layers，审阅时看得到。
LARGE_LAYER_FRACTION = 0.10

FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msjhbd.ttc",
    "C:/Windows/Fonts/msjh.ttc",
)


def find_font(candidates=FONT_CANDIDATES):
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("找不到中文字型：" + ", ".join(candidates))


class OverlapError(Exception):
    pass


class Pen:
    """ImageDraw 的包装：座标按最终尺寸写，自动乘上放大倍数；线宽、半径、字号也一起放大。"""

    def __init__(self, image, scale, font_path=None):
        self.image = image
        self.scale = scale
        self.draw = ImageDraw.Draw(image, "RGBA")
        self.font_path = font_path

    def _p(self, points):
        s = self.scale
        if points and isinstance(points[0], (tuple, list)):
            return [(x * s, y * s) for x, y in points]
        return [v * s for v in points]

    def _shape(self, method, coords, *args, fill=None, outline=None, **kwargs):
        """直接画会把半透明颜色「写进」这一层（底下的东西被换成半透明，像挖了洞）。
        有半透明颜色时，先画在一小块临时图上，再叠上去。"""
        translucent = any(c is not None and len(c) == 4 and c[3] < 255 for c in (fill, outline))
        if fill is not None:
            kwargs["fill"] = fill
        if outline is not None:
            kwargs["outline"] = outline
        if not translucent:
            getattr(self.draw, method)(coords, *args, **kwargs)
            return
        flat = [v for point in coords for v in point] if isinstance(coords[0], (tuple, list)) else list(coords)
        xs, ys = flat[0::2], flat[1::2]
        pad = kwargs.get("width", 1) + 2
        x0, y0 = max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad)
        x1, y1 = min(self.image.width, int(max(xs)) + pad + 1), min(self.image.height, int(max(ys)) + pad + 1)
        if x1 <= x0 or y1 <= y0:
            return
        scratch = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        if isinstance(coords[0], (tuple, list)):
            moved = [(x - x0, y - y0) for x, y in coords]
        else:
            moved = [v - (x0 if i % 2 == 0 else y0) for i, v in enumerate(coords)]
        getattr(ImageDraw.Draw(scratch), method)(moved, *args, **kwargs)
        self.image.alpha_composite(scratch, dest=(x0, y0))

    def ellipse(self, box, fill=None, outline=None, width=1):
        self._shape("ellipse", self._p(box), fill=fill, outline=outline, width=round(width * self.scale))

    def circle(self, center, radius, **kwargs):
        x, y = center
        self.ellipse([x - radius, y - radius, x + radius, y + radius], **kwargs)

    def rectangle(self, box, fill=None, outline=None, width=1):
        self._shape("rectangle", self._p(box), fill=fill, outline=outline, width=round(width * self.scale))

    def rounded_rectangle(self, box, radius, fill=None, outline=None, width=1):
        self._shape("rounded_rectangle", self._p(box), radius=radius * self.scale, fill=fill, outline=outline,
                    width=round(width * self.scale))

    def polygon(self, points, fill=None, outline=None, width=1):
        self._shape("polygon", self._p(points), fill=fill, outline=outline, width=round(width * self.scale))

    def line(self, points, fill=None, width=1, joint="curve"):
        self._shape("line", self._p(points), fill=fill, width=max(1, round(width * self.scale)), joint=joint)

    def arc(self, box, start, end, fill=None, width=1):
        self._shape("arc", self._p(box), start, end, fill=fill, width=max(1, round(width * self.scale)))

    def pieslice(self, box, start, end, fill=None, outline=None, width=1):
        self._shape("pieslice", self._p(box), start, end, fill=fill, outline=outline, width=round(width * self.scale))

    def chord(self, box, start, end, fill=None, outline=None, width=1):
        self._shape("chord", self._p(box), start, end, fill=fill, outline=outline, width=round(width * self.scale))

    def text(self, xy, text, size, fill, anchor="mm", stroke=0, stroke_fill=None):
        font = ImageFont.truetype(self.font_path or find_font(), round(size * self.scale))
        self.draw.text(
            self._p(xy), text, font=font, fill=fill, anchor=anchor,
            stroke_width=round(stroke * self.scale), stroke_fill=stroke_fill,
        )

    def bezier(self, points, fill=None, width=1, steps=48):
        """二次或三次贝塞尔曲线（3 或 4 个控制点），画成折线。"""
        def at(t):
            pts = list(points)
            while len(pts) > 1:
                pts = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1]) for a, b in zip(pts, pts[1:])]
            return pts[0]
        self.line([at(i / steps) for i in range(steps + 1)], fill=fill, width=width)


class Layer:
    def __init__(self, name, z, draw=None, image=None, at=None, blur=0, tint=None, opacity=1.0, expected_holes=0,
                 holes_override=None, part=None, cut_sides=None, cut_edges_ok=()):
        self.name, self.z, self.draw, self.source, self.at = name, z, draw, image, at
        self.blur, self.tint, self.opacity = blur, tint, opacity
        # 零件本来就有、故意留的透明洞像素数（来自零件封印，已按缩放换算）；自检用。
        self.expected_holes = expected_holes
        self.holes_override = holes_override   # (数字, 理由) 或 None；有就会在自检里标成问题
        self.part = part                       # 零件 png 路径（来自 load_part）或 None
        # 贴上去之前（还没补透明边）量的：每一边被裁切框切到几个像素；以及封印里写明故意裁掉的边。
        self.cut_sides = cut_sides or {}
        self.cut_edges_ok = tuple(cut_edges_ok)


class Scene:
    def __init__(self, width, height, scale=4, font_path=None):
        self.width, self.height, self.scale = width, height, scale
        self.font_path = font_path
        self.layers = {}
        self.rules = []
        self.allowances = []      # allow_overlap 声明：(层名或万用字元, 层名或万用字元, 理由)
        self._cache = {}

    def _forget(self, name):
        self._cache.pop(name, None)
        self._cache.pop(("box", name), None)

    # ---- 加层 ----
    def vector(self, name, z, blur=0, opacity=1.0):
        """装饰器：@scene.vector("trees", z=2) 底下的函数收到 pen，画那一层。"""
        def register(function):
            self.layers[name] = Layer(name, z, draw=function, blur=blur, opacity=opacity)
            self._forget(name)
            return function
        return register

    def image(self, name, z, image=None, feet=None, center=None, top_left=None, height=None, scale=None, tint=None,
              holes_override=None, part=None, **unexpected):
        """贴一张透明图。第三个参数（也可以写 part=）给 load_part() 读进来的零件（人物一定要这样），
        预期洞数会自动带入；一般没有洞的小图也可以直接给 PIL Image。用 feet=(x, y) 指定脚踩的位置最直觉。
        height 或 scale 决定大小；tint=((r, g, b), 强度) 把颜色往环境光拉，跟背景统一色调。
        holes_override=(数字, "理由") 是手动改预期洞数：会记进 check.json，而且自检会当成问题、publish 不出图。"""
        if "expected_holes" in unexpected:
            raise TypeError("不要自己填 expected_holes：用 load_part() 读零件，预期洞数会自动带入")
        if unexpected:
            raise TypeError(f"scene.image() 不认得这些参数：{sorted(unexpected)}")
        if part is not None:
            if image is not None:
                raise TypeError("scene.image() 的图只能给一次：第三个参数或 part=，二选一")
            image = part
        if image is None:
            raise TypeError("scene.image() 要给一张图：scene.image(名字, z, load_part(...), feet=...)")
        part_path = None
        expected_holes = 0
        cut_edges_ok = ()
        if isinstance(image, Part):
            part = verify(image)   # 真的是 load_part() 读的、读进来后没被改过；否则报错
            part_path, expected_holes, image = part.path, part.expected_holes, part.image
            cut_edges_ok = part.cut_edges_ok
        elif not isinstance(image, Image.Image):
            raise TypeError("scene.image() 只收 load_part() 读的零件，或一张 PIL 图（没有洞的小道具）")
        if holes_override is not None:
            if (not isinstance(holes_override, tuple) or len(holes_override) != 2
                    or type(holes_override[0]) is not int or not isinstance(holes_override[1], str)
                    or not holes_override[1].strip()):
                raise TypeError('holes_override 要写成 (整数, "理由")')
            expected_holes = holes_override[0]
        image = image.convert("RGBA")
        cut_sides = cut_edges(image)   # 补透明边之前量，不然直线断面会被透明边盖掉
        box = image.getbbox() or (0, 0, image.width, image.height)
        # 裁到人物范围再留 2 像素透明边，缩放时边缘才有地方过渡，自检也分得出「被裁切框切到」。
        pad = 2
        image = image.crop((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
        if height:
            scale = height / (box[3] - box[1])
        expected_holes = expected_holes * (scale or 1) ** 2
        if scale and scale != 1:
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        margin = pad * (scale or 1)
        if feet:
            at = (round(feet[0] - image.width / 2), round(feet[1] - image.height + margin))
        elif center:
            at = (round(center[0] - image.width / 2), round(center[1] - image.height / 2))
        else:
            at = tuple(round(v) for v in (top_left or (0, 0)))
        self.layers[name] = Layer(name, z, image=image, at=at, tint=tint, expected_holes=expected_holes,
                                  holes_override=holes_override, part=part_path, cut_sides=cut_sides,
                                  cut_edges_ok=cut_edges_ok)
        self._forget(name)

    def forbid_overlap(self, top, bottom, margin=0):
        """合成时检查：top 层不准压到 bottom 层（不透明部分有交集就报错）。
        名字可以用万用字元，例如 forbid_overlap("candy-*", "tomb-*")：每一对都检查。"""
        self.rules.append((top, bottom, margin))

    def forbid_overlap_within(self, pattern, margin=0):
        """同一类的东西彼此不准叠，例如每块墓碑各画成一层 tomb-1、tomb-2……，再 forbid_overlap_within("tomb-*")。"""
        self.rules.append((pattern, pattern, margin))

    def allow_overlap(self, a, b, reason):
        """声明 a 和 b 这两层「故意重叠」（例如人物前面的道具、人物身后的太阳）。只用来消掉自检的
        「没设规则的重叠」提醒：不会解除 forbid_overlap，同一对要是也被 forbid_overlap 管着，照样不准叠。
        理由一定要写；名字可以用万用字元，但两边不能都是 *（那等于全部放行）。声明会记进 check.json。"""
        if not isinstance(reason, str) or not reason.strip():
            raise TypeError('allow_overlap 要写理由：allow_overlap("bubble", "character", "泡泡故意飘在她脸前")')
        for pattern in (a, b):
            if not isinstance(pattern, str) or not pattern:
                raise TypeError("allow_overlap 的层名要是字串")
        if all(not p.strip("*?") for p in (a, b)):
            raise TypeError("allow_overlap 两边不能都是万用字元（那就是全部放行）；请写出至少一边的名字")
        self.allowances.append((a, b, reason.strip()))

    def _match(self, pattern):
        """名字或万用字元对到的现有图层（分大小写）。没有 * ? [ 就照字面比。"""
        if not any(c in pattern for c in "*?["):
            return [pattern] if pattern in self.layers else []
        return sorted(n for n in self.layers if fnmatch.fnmatchcase(n, pattern))

    def rule_problems(self):
        """规则本身的问题（fail closed）：任一边一层都没对到（多半是名字写错，例如 candy-* 对不上 candy），
        或一边对到超过 MAX_RULE_LAYERS 层。这些规则不会被检查，所以一律当成自检问题。"""
        problems = []
        for top, bottom, margin in self.rules:
            sides = (("上层", top), ("下层", bottom)) if top != bottom else (("", top),)
            for side, pattern in sides:
                found = self._match(pattern)
                if not found:
                    problems.append({"rule": [top, bottom, margin], "pattern": pattern, "matched": 0,
                                     "message": f"规则 {top} / {bottom} 的{side} {pattern!r} 一层都没对到（名字写错？），这条没检查到"})
                elif len(found) > MAX_RULE_LAYERS:
                    problems.append({"rule": [top, bottom, margin], "pattern": pattern, "matched": len(found),
                                     "message": f"规则 {top} / {bottom} 的{side} {pattern!r} 对到 {len(found)} 层，超过上限 "
                                                f"{MAX_RULE_LAYERS}；请合并或分组，这条没检查到"})
            if top == bottom and not any(c in top for c in "*?["):
                problems.append({"rule": [top, bottom, margin], "pattern": top, "matched": 1,
                                 "message": f"forbid_overlap_within({top!r}) 只是一个名字、不是一类（要用 * ），这条没检查到"})
        return problems

    def _pairs(self):
        """把规则展开成实际的 (top, bottom, margin)，万用字元对到现有的层；同一层不跟自己比，同一对只比一次。
        有问题的规则（见 rule_problems）不展开。"""
        seen = set()
        bad = {tuple(p["rule"]) for p in self.rule_problems()}
        for top, bottom, margin in self.rules:
            if (top, bottom, margin) in bad:
                continue
            tops, bottoms = self._match(top), self._match(bottom)
            for a in tops:
                for b in bottoms:
                    key = (a, b, margin) if a <= b or top != bottom else (b, a, margin)
                    if a == b or key in seen:
                        continue
                    seen.add(key)
                    yield a, b, margin

    def _allowed_pairs(self):
        """allow_overlap 展开成 {frozenset((a, b)): 理由}。一边对到超过 MAX_RULE_LAYERS 层的不展开
        （见 allowance_warnings）；同一层不跟自己配。"""
        allowed = {}
        for a, b, reason in self.allowances:
            tops, bottoms = self._match(a), self._match(b)
            if len(tops) > MAX_RULE_LAYERS or len(bottoms) > MAX_RULE_LAYERS:
                continue
            for x in tops:
                for y in bottoms:
                    if x != y:
                        allowed.setdefault(frozenset((x, y)), reason)
        return allowed

    def allowance_warnings(self):
        """allow_overlap 声明本身的问题：一边没对到任何图层（名字写错），或一边对到太多层而没有生效。"""
        found = []
        for a, b, reason in self.allowances:
            for pattern in (a, b):
                matched = self._match(pattern)
                if not matched:
                    found.append(f"allow_overlap({a!r}, {b!r}) 的 {pattern!r} 一层都没对到（名字写错？），这条声明没有生效")
                elif len(matched) > MAX_RULE_LAYERS:
                    found.append(f"allow_overlap({a!r}, {b!r}) 的 {pattern!r} 对到 {len(matched)} 层，超过上限 "
                                 f"{MAX_RULE_LAYERS}，这条声明没有生效")
        return found

    # ---- 算图 ----
    def layer_image(self, name):
        if name in self._cache:
            return self._cache[name]
        layer = self.layers[name]
        if layer.draw:
            big = Image.new("RGBA", (self.width * self.scale, self.height * self.scale), (0, 0, 0, 0))
            layer.draw(Pen(big, self.scale, self.font_path))
            if layer.blur:
                big = big.filter(ImageFilter.GaussianBlur(layer.blur * self.scale))
            # 预乘透明度再缩小，半透明边缘不会出现黑边或白边。
            out = big.convert("RGBa").resize((self.width, self.height), Image.LANCZOS).convert("RGBA")
        else:
            out = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            source = layer.source
            if layer.tint:
                # 正片叠底往环境光的颜色拉，强度 0–1：夜景就带一点紫，人物不会像贴纸一样亮。
                colour, strength = layer.tint
                rgb = source.convert("RGB")
                lit = ImageChops.multiply(rgb, Image.new("RGB", source.size, tuple(colour)))
                mixed = Image.blend(rgb, lit, strength).convert("RGBA")
                mixed.putalpha(source.getchannel("A"))
                source = mixed
            out.alpha_composite(source, dest=(max(0, layer.at[0]), max(0, layer.at[1])),
                                source=(max(0, -layer.at[0]), max(0, -layer.at[1])))
        if layer.opacity < 1:
            alpha = out.getchannel("A").point(lambda v: round(v * layer.opacity))
            out.putalpha(alpha)
        self._cache[name] = out
        return out

    def mask(self, name, threshold=0):
        """这一层「看得见」的范围：透明度大于 threshold 的像素。

        预设 0，也就是只要有一点覆盖就算。理由：合成后的颜色是 8 位整数，叠上去的颜色和底下差 Δ 时，
        透明度 a 的像素会让结果变 a×Δ/255；颜色差很大时（例如黄糖果叠在深蓝头发上，Δ≈200），a=2 就会
        改变像素值，所以没有一个「保证看不出来」的正数门槛。重叠检查、找空位都用同一个门槛，
        找到的空位就一定过得了重叠检查。"""
        return np.asarray(self.layer_image(name).getchannel("A")) > threshold

    def bbox(self, name):
        return self.layer_image(name).getbbox()

    def anchor(self, name, where="feet"):
        """某层不透明部分的位置：feet（底部中间）、top、left、right、center。"""
        left, top, right, bottom = self.bbox(name)
        return {
            "feet": ((left + right) / 2, bottom),
            "top": ((left + right) / 2, top),
            "left": (left, (top + bottom) / 2),
            "right": (right, (top + bottom) / 2),
            "center": ((left + right) / 2, (top + bottom) / 2),
        }[where]

    def free_spot(self, size, near, avoid, margin=6, within=None, taken=()):
        """在 near 附近找一块 size=(宽, 高) 的空位，不碰 avoid 那几层（外扩 margin 像素），也不碰 taken 里已经
        用掉的框。回传左上角座标；找不到就报错。由近到远一圈一圈找。"""
        width, height = size
        blocked = np.zeros((self.height, self.width), bool)
        for name in avoid:
            blocked |= self.mask(name)
        if margin:
            blocked = ndimage.binary_dilation(blocked, iterations=margin)
        for x0, y0, x1, y1 in taken:
            blocked[max(0, int(y0) - margin):int(y1) + margin, max(0, int(x0) - margin):int(x1) + margin] = True
        left, top, right, bottom = within or (0, 0, self.width, self.height)
        integral = blocked.astype(np.int32).cumsum(0).cumsum(1)

        def free(x, y):
            if x < left or y < top or x + width > right or y + height > bottom:
                return False
            a = integral[y + height - 1, x + width - 1]
            b = integral[y - 1, x + width - 1] if y > 0 else 0
            c = integral[y + height - 1, x - 1] if x > 0 else 0
            d = integral[y - 1, x - 1] if x > 0 and y > 0 else 0
            return a - b - c + d == 0

        cx, cy = round(near[0] - width / 2), round(near[1] - height / 2)
        for radius in range(0, max(self.width, self.height), 2):
            for dx in range(-radius, radius + 1, 2):
                for dy in (-radius, radius) if abs(dx) != radius else range(-radius, radius + 1, 2):
                    if free(cx + dx, cy + dy):
                        return (cx + dx, cy + dy)
        raise OverlapError(f"{near} 附近找不到 {size} 的空位")

    def _visible_box(self, name):
        """这层看得见的范围的外框 (x0, y0, x1, y1)，没有就 None。跟 mask() 同一个门槛。"""
        key = ("box", name)
        if key not in self._cache:
            ys, xs = np.nonzero(self.mask(name))
            self._cache[key] = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if len(xs) else None
        return self._cache[key]

    def _hit(self, top, bottom, margin=0):
        """top 的看得见范围（alpha > 0）跟 bottom（外扩 margin）有没有交集：先比外框，碰得到才在交集那一小块比像素。
        有就回传 {"top", "bottom", "pixels", "bbox"}，没有回传 None。forbid_overlap 与「没设规则」的提醒共用这里，
        所以两边的「重叠」是同一个定义。"""
        a, b = self._visible_box(top), self._visible_box(bottom)
        if a is None or b is None:
            return None
        x0, y0 = max(a[0], b[0] - margin), max(a[1], b[1] - margin)
        x1, y1 = min(a[2], b[2] + margin), min(a[3], b[3] + margin)
        if x0 >= x1 or y0 >= y1:
            return None
        # 下层多取 margin 像素再外扩，交集里的结果跟整张画布外扩一样。
        px0, py0 = max(0, x0 - margin), max(0, y0 - margin)
        px1, py1 = min(self.width, x1 + margin), min(self.height, y1 + margin)
        under = self.mask(bottom)[py0:py1, px0:px1]
        if margin:
            under = ndimage.binary_dilation(under, iterations=margin)
        hit = self.mask(top)[y0:y1, x0:x1] & under[y0 - py0:y1 - py0, x0 - px0:x1 - px0]
        if not hit.any():
            return None
        ys, xs = np.nonzero(hit)
        return {"top": top, "bottom": bottom, "pixels": int(hit.sum()),
                "bbox": [int(xs.min()) + x0, int(ys.min()) + y0, int(xs.max()) + 1 + x0, int(ys.max()) + 1 + y0]}

    def overlaps(self):
        problems = []
        for top, bottom, margin in self._pairs():
            found = self._hit(top, bottom, margin)
            if found:
                problems.append(found)
        return problems

    def unruled_overlaps(self, characters=()):
        """人物层（用 load_part 读的零件层，加上 characters 里点名的层）跟别的层重叠，却没有设规则的情形。

        一对层只要符合下面任一条就不提醒：forbid_overlap 系的规则管着这一对（不管会不会触发）、allow_overlap 声明了
        故意重叠、另一层是大的场景层（超过画面 LARGE_LAYER_FRACTION，会记在 skipped_large）。重叠的定义跟
        forbid_overlap 一样（alpha > 0）。回传 {"unruled": [...], "allowed": [...], "skipped_large": [...]}，
        每个 unruled 项有 top / bottom（照 z 顺序）、pixels、bbox、character（哪一层是人物）、other。"""
        order = {name: index for index, name in enumerate(self.layers)}
        stack = lambda name: (self.layers[name].z, order[name])   # noqa: E731 - 跟 render 的叠放顺序一样
        people = sorted({n for n, layer in self.layers.items() if layer.part is not None}
                        | {n for n in characters if n in self.layers}, key=order.get)
        ruled = {frozenset((a, b)) for a, b, _ in self._pairs()}
        allowed = self._allowed_pairs()
        canvas = self.width * self.height
        result = {"unruled": [], "allowed": [], "skipped_large": []}
        seen = set()
        for person in people:
            for other in self.layers:
                key = frozenset((person, other))
                if other == person or key in seen:
                    continue
                seen.add(key)
                if key in ruled:
                    continue
                if self.layers[other].part is None and int(self.mask(other).sum()) > canvas * LARGE_LAYER_FRACTION:
                    result["skipped_large"].append(other)
                    continue
                low, high = sorted((person, other), key=stack)
                found = self._hit(high, low)
                if not found:
                    continue
                found = {**found, "character": person, "other": other}
                if key in allowed:
                    result["allowed"].append({**found, "reason": allowed[key]})
                else:
                    result["unruled"].append(found)
        result["skipped_large"] = sorted(set(result["skipped_large"]))
        return result

    def render(self, check=True):
        if check:
            messages = [p["message"] for p in self.rule_problems()]
            messages += [f"{p['top']} 压到 {p['bottom']}：{p['pixels']} 像素，范围 {p['bbox']}" for p in self.overlaps()]
            if messages:
                raise OverlapError("; ".join(messages))
        out = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 255))
        for layer in sorted(self.layers.values(), key=lambda item: item.z):
            out.alpha_composite(self.layer_image(layer.name))
        return out

    def save_layers(self, folder):
        os.makedirs(folder, exist_ok=True)
        for name in self.layers:
            self.layer_image(name).save(os.path.join(folder, f"layer-{name}.png"))
