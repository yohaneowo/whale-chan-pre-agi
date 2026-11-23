import contextlib
import dataclasses
import io
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from whaledraw import parts
from whaledraw.check import CheckFailed, character_report, enclosed_sheet, publish, self_check
from whaledraw.cutout import cut_out, edge_quality
from whaledraw.parts import Part, PartError, load_part
from whaledraw.scene import MAX_RULE_LAYERS, OverlapError, Scene

POSIX = os.name == "posix"
HERE = os.path.dirname(os.path.abspath(__file__))

# 真实样本（她的人物设定图 whale_sheet.png）不放进公开 repo。要跑那一条，设 WHALEDRAW_SAMPLES 指到有这个档的资料夹；
# 本机共享样本：C:\Users\USER\OpenBot\Shared\whale-draw-acceptance-20261101\samples
SAMPLES = os.environ.get("WHALEDRAW_SAMPLES")
SAMPLE_SHEET = os.path.join(SAMPLES or "", "whale_sheet.png")


def figure(scale=4):
    """白底上的假人物：深色描边的蓝色圆身体，中间一块白色「围裙」（被描边围住，要保留），
    右边一圈蓝色「发丝」围住一块白色空隙（其实是背景），先放大画再缩小，边缘有抗锯齿的混色。"""
    size = 160 * scale
    image = Image.new("RGB", (size, size), (254, 254, 254))
    draw = ImageDraw.Draw(image)
    s = scale
    draw.ellipse([30 * s, 30 * s, 110 * s, 130 * s], fill=(50, 80, 190), outline=(20, 20, 40), width=3 * s)
    draw.ellipse([52 * s, 60 * s, 88 * s, 100 * s], fill=(250, 250, 252), outline=(20, 20, 40), width=2 * s)
    draw.ellipse([100 * s, 40 * s, 150 * s, 90 * s], outline=(20, 20, 40), width=8 * s)
    draw.ellipse([102 * s, 42 * s, 148 * s, 88 * s], outline=(60, 110, 220), width=4 * s)
    return image.resize((160, 160), Image.LANCZOS)


def threshold_cut(image):
    """她以前的做法：接近白色的像素一律透明。"""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, _ = pixels[x, y]
            if r > 243 and g > 243 and b > 243:
                pixels[x, y] = (255, 255, 255, 0)
    return rgba


class CutoutTest(unittest.TestCase):
    def test_keeps_enclosed_white_and_has_no_halo(self):
        cut = cut_out(figure())
        alpha = cut.alpha
        self.assertEqual(alpha[80, 70], 255, "围裙中间要保留")
        self.assertEqual(alpha[65, 125], 255, "没指定的空隙一律保留")
        quality = edge_quality(cut.image)
        self.assertLess(quality["halo"], 0.02)
        self.assertEqual(quality["specks"], 0)
        self.assertEqual(quality["hole_pixels"], 0)
        self.assertTrue(cut.report["enclosed"], "被围住的白色要列在报告里")

    def test_seed_removes_only_that_gap(self):
        cut = cut_out(figure(), background_seeds=[(125, 65)])
        self.assertEqual(cut.alpha[65, 125], 0, "指定的空隙挖掉")
        self.assertEqual(cut.alpha[80, 70], 255, "围裙不受影响")
        self.assertEqual(cut.report["unmatched_seeds"], [])
        self.assertGreater(cut.report["removed_pixels"], 0)

    def test_seed_slightly_off_still_matches(self):
        cut = cut_out(figure(), background_seeds=[(128, 60)])
        self.assertEqual(cut.alpha[65, 125], 0)

    def test_unknown_seed_is_reported(self):
        cut = cut_out(figure(), background_seeds=[(38, 80), (500, 500)])
        self.assertEqual(cut.alpha[80, 38], 255, "点在身体颜色上（附近没有白色空隙）就不挖")
        self.assertEqual(cut.alpha[80, 70], 255)
        self.assertEqual(cut.report["unmatched_seeds"], [[38, 80], [500, 500]])

    def test_old_threshold_method_is_flagged(self):
        quality = edge_quality(threshold_cut(figure()))
        self.assertGreater(quality["halo"], 0.2, "白边量得出来")
        self.assertGreater(quality["hole_pixels"], 100, "围裙被挖空量得出来")

    def test_edge_colour_is_unmixed(self):
        cut = cut_out(figure())
        rgba = np.asarray(cut.image).astype(int)
        edge = (rgba[..., 3] > 0) & (rgba[..., 3] < 200)
        self.assertTrue(edge.any())
        self.assertLess(rgba[edge][:, :3].min(axis=1).mean(), 150, "半透明的边缘是线稿色，不是白色")

    def test_transparent_input_is_refused(self):
        rgba = figure().convert("RGBA")
        rgba.putpixel((0, 0), (254, 254, 254, 0))
        with self.assertRaises(ValueError):
            cut_out(rgba)
        opaque = figure().convert("RGBA")
        self.assertEqual(cut_out(opaque).alpha[80, 70], 255, "完全不透明的 RGBA 照常收")

    def test_crop_touching_figure_is_reported(self):
        cut = cut_out(figure(), crop=(0, 0, 100, 160))
        self.assertGreater(character_report(cut.image)["cut_edge_pixels"], 0)


class TrustedParts:
    """测试用的信任零件目录（照 0.2.1 的格局）：
        <tmp>/opt/dsh/whaledraw-parts/20261102-1/   一组零件
        <tmp>/opt/dsh/whaledraw-parts/current -> 20261102-1
    拥有者是跑测试的帐号，封印格式用 parts.seal_record（跟 seal_part.py 同一份）。"""

    SET = "20261102-1"

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.top = os.path.join(self.root, "opt", "dsh")
        self.parts_root = os.path.join(self.top, "whaledraw-parts")
        self.folder = os.path.join(self.parts_root, self.SET)
        os.makedirs(self.folder)
        for path in (os.path.join(self.root, "opt"), self.top, self.parts_root, self.folder):
            os.chmod(path, 0o755)
        self.current = os.path.join(self.parts_root, "current")
        os.symlink(self.SET, self.current)
        self.patch = mock.patch.multiple(parts, PARTS_ROOT=self.parts_root, TRUSTED_TOP=self.top,
                                         TRUSTED_UID=os.getuid())
        self.patch.start()

    def close(self):
        self.patch.stop()
        self.tmp.cleanup()

    def add(self, image, name="part.png", holes=None, folder=None, pose="front", cut_edges_ok=()):
        """封印一张图；放在信任目录时回传「经过 current」的路径（她实际会用的那条）。"""
        folder = folder or self.folder
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        image.save(path)
        with open(path, "rb") as file:
            raw = file.read()
        record = parts.seal_record(raw, name, pose, cut_edges_ok)
        if holes is not None:
            record["expected_hole_pixels"] = holes
        sidecar = os.path.splitext(path)[0] + ".json"
        with open(sidecar, "w", encoding="utf-8") as file:
            json.dump(record, file)
        for item in (path, sidecar):
            os.chmod(item, 0o644)
        return os.path.join(self.current, name) if folder == self.folder else path

    def rewrite_json(self, path, **changes):
        sidecar = os.path.splitext(os.path.realpath(path))[0] + ".json"
        with open(sidecar, encoding="utf-8") as file:
            data = json.load(file)
        data.update(changes)
        with open(sidecar, "w", encoding="utf-8") as file:
            json.dump(data, file)


def person_image():
    person = Image.new("RGBA", (44, 84), (0, 0, 0, 0))   # 留透明边：碰到图边会被当成「被裁切框切到」
    ImageDraw.Draw(person).ellipse([2, 2, 41, 81], fill=(200, 80, 80, 255))
    return person


@unittest.skipUnless(POSIX, "零件要检查拥有者与权限，只在 Linux 跑（容器或 VM）")
class SceneTest(unittest.TestCase):
    def setUp(self):
        self.trust = TrustedParts()
        self.person = load_part(self.trust.add(person_image(), "person.png"))

    def tearDown(self):
        self.trust.close()

    def scene(self):
        scene = Scene(200, 120, scale=4)

        @scene.vector("sky", z=0)
        def sky(pen):
            pen.rectangle([0, 0, 200, 120], fill=(30, 20, 60, 255))
            pen.ellipse([20, 20, 60, 60], fill=(255, 255, 255, 120))

        scene.image("character", 10, self.person, feet=(100, 110))
        return scene

    def test_vector_layers_are_antialiased_and_translucent_shapes_blend(self):
        scene = self.scene()
        sky = scene.layer_image("sky")
        self.assertEqual(sky.getpixel((40, 40))[3], 255, "半透明的圆叠在不透明的天空上，这层不能被挖成半透明")
        values = {sky.getpixel((x, 40))[0] for x in range(15, 26)}
        self.assertGreater(len(values), 3, "圆的边缘有过渡色（抗锯齿）")

    def test_render_is_repeatable(self):
        a = self.scene().render().tobytes()
        b = self.scene().render().tobytes()
        self.assertEqual(a, b)

    def test_anchor_and_free_spot(self):
        scene = self.scene()
        x, y = scene.anchor("character", "feet")
        self.assertAlmostEqual(x, 100, delta=2)
        self.assertAlmostEqual(y, 110, delta=2)
        left, top = scene.free_spot((20, 10), near=(100, 100), avoid=["character"], margin=2)
        box = scene.mask("character")[top:top + 10, left:left + 20]
        self.assertFalse(box.any(), "找到的空位不压人物")

    def test_forbidden_overlap_blocks_render(self):
        scene = self.scene()

        @scene.vector("candy", z=20)
        def candy(pen):
            pen.circle((100, 80), 6, fill=(240, 200, 60, 255))

        scene.forbid_overlap("candy", "character")
        with self.assertRaises(OverlapError):
            scene.render()

    def test_faint_overlap_is_caught(self):
        """Rin 的复现：alpha=20 的糖果叠在人物上，合成像素已经变了，重叠检查必须报。"""
        scene = self.scene()

        @scene.vector("candy", z=20)
        def candy(pen):
            pen.circle((100, 80), 6, fill=(240, 200, 60, 20))

        scene.forbid_overlap("candy", "character")
        before = self.scene().render(check=False).getpixel((100, 80))
        after = scene.render(check=False).getpixel((100, 80))
        self.assertNotEqual(before, after, "合成像素确实变了")
        self.assertTrue(scene.overlaps(), "看得见的覆盖就算重叠")
        with self.assertRaises(OverlapError):
            scene.render()

    def test_free_spot_result_passes_overlap_check(self):
        scene = self.scene()
        left, top = scene.free_spot((14, 14), near=(100, 80), avoid=["character"], margin=1)

        @scene.vector("candy", z=20)
        def candy(pen):
            pen.ellipse([left, top, left + 14, top + 14], fill=(240, 200, 60, 255))

        scene.forbid_overlap("candy", "character")
        self.assertEqual(scene.overlaps(), [])

    def test_publish_refuses_failed_picture(self):
        scene = self.scene()

        @scene.vector("candy", z=20)
        def candy(pen):
            pen.circle((100, 80), 6, fill=(240, 200, 60, 255))

        scene.forbid_overlap("candy", "character")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.png")
            Image.new("RGB", (4, 4)).save(path)  # 上一次的旧成品
            with self.assertRaises(CheckFailed) as caught:
                publish(scene, path, os.path.join(folder, "check"))
            self.assertFalse(os.path.exists(path), "没过关就不留成品，旧的也删掉")
            self.assertTrue(os.path.exists(caught.exception.rejected))
            self.assertTrue(caught.exception.rejected.endswith("out.REJECTED.png"))
            self.assertIn("candy 压到 character", caught.exception.problems[0])

    def test_publish_writes_passing_picture(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.png")
            final, report = publish(self.scene(), path, os.path.join(folder, "check"))
            self.assertTrue(os.path.exists(path))
            self.assertEqual(report["problems"], [])

    def test_self_check_writes_sheets(self):
        scene = self.scene()
        with tempfile.TemporaryDirectory() as folder:
            report = self_check(scene, folder)
            for name in ("check-grid.png", "check-edges.png", "check-overlap.png", "check.json"):
                self.assertTrue(os.path.exists(os.path.join(folder, name)), name)
            self.assertEqual(report["problems"], [])

    def test_wildcard_and_within_rules_catch_stacked_props(self):
        """墓碑叠在一起、糖果压在墓碑上，以前没有规则能挡（同一层里互相叠看不出来）。"""
        scene = self.scene()
        scene.vector("tomb-1", z=5)(lambda pen: pen.rectangle([10, 60, 40, 100], fill=(150, 150, 160, 255)))
        scene.vector("tomb-2", z=5)(lambda pen: pen.rectangle([30, 70, 60, 110], fill=(150, 150, 160, 255)))
        scene.vector("tomb-3", z=5)(lambda pen: pen.rectangle([150, 60, 180, 100], fill=(150, 150, 160, 255)))
        scene.vector("candy", z=6)(lambda pen: pen.circle((165, 62), 4, fill=(240, 90, 120, 255)))
        scene.forbid_overlap_within("tomb-*")
        scene.forbid_overlap("candy", "tomb-*")
        found = {(o["top"], o["bottom"]) for o in scene.overlaps()}
        self.assertEqual(found, {("tomb-1", "tomb-2"), ("candy", "tomb-3")})

    def test_rule_that_matches_nothing_fails_closed(self):
        """Rin 的复现：candy 明显压到人物，规则写成 candy-*（名字对不上），以前 overlaps() 回空、照样出图。"""
        scene = self.scene()
        scene.vector("candy", z=20)(lambda pen: pen.circle((100, 80), 6, fill=(240, 200, 60, 255)))
        scene.forbid_overlap("candy-*", "character")
        self.assertEqual(scene.overlaps(), [], "这条规则确实对不到任何一对")
        problems = scene.rule_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("'candy-*' 一层都没对到", problems[0]["message"])
        with self.assertRaises(OverlapError):
            scene.render()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(CheckFailed) as caught:
                publish(scene, os.path.join(folder, "out.png"), os.path.join(folder, "check"))
            self.assertTrue(any("没对到" in p for p in caught.exception.problems))
            with open(os.path.join(folder, "check", "check.json"), encoding="utf-8") as file:
                saved = json.load(file)
            self.assertEqual(saved["unchecked_rules"][0]["pattern"], "candy-*")
            self.assertFalse(os.path.exists(os.path.join(folder, "out.png")))

    def test_misspelled_plain_name_and_single_name_within_are_problems(self):
        scene = self.scene()
        scene.vector("candy", z=20)(lambda pen: pen.circle((10, 10), 3, fill=(240, 200, 60, 255)))
        scene.forbid_overlap("candy", "charactor")            # 拼错
        scene.forbid_overlap_within("candy")                  # 只有一个名字，不是一类
        messages = [p["message"] for p in scene.rule_problems()]
        self.assertTrue(any("'charactor' 一层都没对到" in m for m in messages), messages)
        self.assertTrue(any("只是一个名字" in m for m in messages), messages)
        with self.assertRaises(OverlapError):
            scene.render()
        ok = self.scene()
        ok.vector("candy", z=20)(lambda pen: pen.circle((10, 10), 3, fill=(240, 200, 60, 255)))
        ok.forbid_overlap("candy", "character")
        self.assertEqual(ok.rule_problems(), [])
        ok.render()

    def test_within_rule_has_a_layer_cap(self):
        """Rin：500 层的 forbid_overlap_within 会展开约 12.5 万对。超过上限就当问题，不展开。"""
        scene = Scene(400, 300, scale=1)
        for n in range(MAX_RULE_LAYERS + 1):
            x, y = 10 + (n % 20) * 19, 10 + (n // 20) * 19
            scene.vector(f"dot-{n}", z=1)(lambda pen, x=x, y=y: pen.rectangle([x, y, x + 6, y + 6], fill=(255, 0, 0, 255)))
        scene.forbid_overlap_within("dot-*")
        problems = scene.rule_problems()
        self.assertEqual(problems[0]["matched"], MAX_RULE_LAYERS + 1)
        self.assertIn("超过上限", problems[0]["message"])
        self.assertEqual(list(scene._pairs()), [], "超过上限的规则不展开")
        with self.assertRaises(OverlapError):
            scene.render()
        del scene.layers[f"dot-{MAX_RULE_LAYERS}"]
        started = time.monotonic()
        self.assertEqual(scene.overlaps(), [], "分开的方块彼此不叠")
        self.assertEqual(len(list(scene._pairs())), MAX_RULE_LAYERS * (MAX_RULE_LAYERS - 1) // 2)
        self.assertLess(time.monotonic() - started, 10, "外框先筛，2016 对很快")

    def test_box_prefilter_gives_the_same_answer_as_the_full_canvas(self):
        rng = np.random.default_rng(5)
        scene = Scene(160, 120, scale=1)
        for n in range(12):
            x, y, r = (int(v) for v in (rng.integers(0, 150), rng.integers(0, 110), rng.integers(2, 14)))
            scene.vector(f"blob-{n}", z=1)(lambda pen, x=x, y=y, r=r: pen.circle((x, y), r, fill=(9, 9, 9, 255)))
        for margin in (0, 1, 4):
            scene.rules = []
            scene.forbid_overlap_within("blob-*", margin=margin)
            fast = {(o["top"], o["bottom"]): (o["pixels"], o["bbox"]) for o in scene.overlaps()}
            slow = {}
            for top, bottom, m in scene._pairs():
                under = ndimage.binary_dilation(scene.mask(bottom), iterations=m) if m else scene.mask(bottom)
                hit = scene.mask(top) & under
                if hit.any():
                    ys, xs = np.nonzero(hit)
                    slow[(top, bottom)] = (int(hit.sum()), [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1])
            self.assertEqual(fast, slow, f"margin={margin}")
            self.assertTrue(slow, "测试里要真的有重叠")

    def test_enclosed_sheet_draws(self):
        sheet = enclosed_sheet(cut_out(figure(), background_seeds=[(125, 65)]))
        self.assertEqual(sheet.size, (320, 320))


@unittest.skipUnless(POSIX, "零件要检查拥有者与权限，只在 Linux 跑（容器或 VM）")
class UnruledOverlapTest(unittest.TestCase):
    """自检提醒：图层跟人物重叠、却没设规则。提醒不挡出图（有些重叠是故意的），写进 check.json 的 warnings，
    在 check-overlap.png 上用橙色标出来。11/2 心情图：泡泡层被人物的头发挡住两颗，她没设 forbid_overlap，自检抓不到。"""

    def setUp(self):
        self.trust = TrustedParts()
        self.person = load_part(self.trust.add(person_image(), "person.png"))

    def tearDown(self):
        self.trust.close()

    def mood_scene(self, bubbles_over_person=True):
        """心情图的缩小版：整张的天空、大的太阳、脚下的地面、一层泡泡（有一颗被人物挡住）、人物压在最上面。"""
        scene = Scene(200, 120, scale=4)
        scene.vector("sky", z=0)(lambda pen: pen.rectangle([0, 0, 200, 120], fill=(245, 240, 250, 255)))
        scene.vector("sun", z=2)(lambda pen: pen.circle((30, 30), 20, fill=(255, 248, 205, 255)))          # 5%：没超过 10%
        scene.vector("ground", z=3)(lambda pen: pen.rectangle([0, 116, 200, 120], fill=(214, 226, 205, 255)))  # 3%；LANCZOS 缩小会让边缘往外多出约 3 像素的淡淡覆盖，所以离人物远一点

        @scene.vector("bubble", z=9)                    # 比人物（z=10）低，所以被人物盖住
        def bubble(pen):
            pen.circle((60, 50), 5, fill=(190, 226, 245, 210))                        # 在人物外面
            if bubbles_over_person:
                pen.circle((84, 60), 6, fill=(190, 226, 245, 210))                    # 被人物挡住

        scene.image("character", 10, self.person, feet=(100, 110))
        return scene

    def report(self, scene, name="c"):
        return self_check(scene, os.path.join(self.trust.root, name))

    def test_mood_picture_case_triggers_the_warning_but_does_not_block(self):
        scene = self.mood_scene()
        report = self.report(scene)
        self.assertEqual(report["problems"], [], "只是提醒，不是错误")
        self.assertEqual(len(report["warnings"]), 1, report["warnings"])
        self.assertIn("「bubble」跟人物「character」重叠了", report["warnings"][0])
        self.assertIn("forbid_overlap('character', 'bubble')", report["warnings"][0], "教她怎么消掉")
        self.assertIn("allow_overlap('character', 'bubble'", report["warnings"][0])
        item = report["unruled_overlaps"][0]
        self.assertEqual((item["character"], item["other"], item["top"], item["bottom"]),
                         ("character", "bubble", "character", "bubble"), "人物压在泡泡上面")
        self.assertGreater(item["pixels"], 0)
        with open(os.path.join(self.trust.root, "c", "check.json"), encoding="utf-8") as file:
            saved = json.load(file)
        self.assertEqual(saved["warnings"], report["warnings"])
        # 橙色标在检查图上
        sheet = np.asarray(Image.open(os.path.join(self.trust.root, "c", "check-overlap.png")).convert("RGB"))
        self.assertTrue(((sheet == (255, 150, 0)).all(axis=2)).any(), "check-overlap.png 上有橙色的重叠")
        # publish 照样出图
        out = os.path.join(self.trust.root, "out.png")
        final, published = publish(scene, out, os.path.join(self.trust.root, "c2"))
        self.assertTrue(os.path.exists(out))
        self.assertEqual(len(published["warnings"]), 1)

    def test_no_overlap_no_warning(self):
        report = self.report(self.mood_scene(bubbles_over_person=False))
        self.assertEqual(report["warnings"], [])
        self.assertNotIn("unruled_overlaps", report)

    def test_forbid_rule_silences_the_warning(self):
        """这一对已经有 forbid_overlap 管着：压到会变成「错误」（problems），不会同时又提醒一次。"""
        scene = self.mood_scene()
        scene.forbid_overlap("bubble", "character")
        report = self.report(scene)
        self.assertEqual(report["warnings"], [])
        self.assertTrue(any("bubble 压到 character" in p for p in report["problems"]), report["problems"])
        clear = self.mood_scene(bubbles_over_person=False)
        clear.forbid_overlap("bubble", "character")
        self.assertEqual(self.report(clear, "c3")["warnings"], [])

    def test_allow_overlap_silences_the_warning_and_is_recorded(self):
        scene = self.mood_scene()
        scene.allow_overlap("bubble", "character", "泡泡故意飘到她脸前面")
        report = self.report(scene)
        self.assertEqual(report["warnings"], [])
        self.assertNotIn("unruled_overlaps", report)
        self.assertEqual(report["allowed_overlaps"][0]["reason"], "泡泡故意飘到她脸前面")
        self.assertEqual(report["problems"], [])

    def test_allow_overlap_never_lifts_a_forbid_rule(self):
        scene = self.mood_scene()
        scene.forbid_overlap("bubble", "character")
        scene.allow_overlap("bubble", "character", "想让它放行")
        report = self.report(scene)
        self.assertTrue(any("bubble 压到 character" in p for p in report["problems"]), "forbid 照样挡")
        with self.assertRaises(OverlapError):
            scene.render()

    def test_wildcards_cover_only_what_they_match(self):
        """通配的规则或声明只管对得到的层，不会静默放行别的层。"""
        scene = self.mood_scene(bubbles_over_person=False)
        scene.vector("bubble-1", z=9)(lambda pen: pen.circle((84, 60), 4, fill=(190, 226, 245, 255)))
        scene.vector("bubble-2", z=9)(lambda pen: pen.circle((115, 60), 4, fill=(190, 226, 245, 255)))
        scene.vector("candy", z=9)(lambda pen: pen.circle((100, 70), 4, fill=(240, 90, 120, 255)))
        report = self.report(scene)
        self.assertEqual(sorted(i["other"] for i in report["unruled_overlaps"]), ["bubble-1", "bubble-2", "candy"])
        scene.allow_overlap("bubble-*", "character", "泡泡")
        report = self.report(scene, "c4")
        self.assertEqual([i["other"] for i in report["unruled_overlaps"]], ["candy"], "candy 对不上 bubble-*，仍然提醒")
        self.assertEqual(sorted(i["other"] for i in report["allowed_overlaps"]), ["bubble-1", "bubble-2"])
        scene.forbid_overlap("can*", "character")
        report = self.report(scene, "c5")
        self.assertEqual(report["warnings"], [], "can* 管到 candy")
        self.assertTrue(any("candy 压到 character" in p for p in report["problems"]))

    def test_allow_overlap_is_validated_and_typos_are_reported(self):
        scene = self.mood_scene()
        for args in (("bubble", "character", ""), ("bubble", "character", "  "), ("bubble", "character", None),
                     ("*", "*", "全部放行"), ("*", "?*", "全部放行"), ("", "character", "x"), (None, "character", "x")):
            with self.assertRaises(TypeError, msg=str(args)):
                scene.allow_overlap(*args)
        scene.allow_overlap("bubbel", "character", "名字写错了")                      # 对不到任何图层
        report = self.report(scene)
        self.assertEqual(len(report["unruled_overlaps"]), 1, "写错的声明没有生效，提醒还在")
        self.assertTrue(any("'bubbel' 一层都没对到" in w for w in report["warnings"]), report["warnings"])
        scene.allow_overlap("*", "character", "一边是万用字元可以，但只放行跟人物的")        # 一边写明就可以
        self.assertEqual(self.report(scene, "c6").get("unruled_overlaps", []), [], "对到 bubble 等跟人物重叠的层")

    def test_same_threshold_as_forbid_overlap(self):
        """重叠的定义跟 forbid_overlap 一样（alpha > 0）：很淡的覆盖也算；贴得很近但没碰到不算。"""
        scene = self.mood_scene(bubbles_over_person=False)
        scene.vector("faint", z=9)(lambda pen: pen.circle((100, 60), 6, fill=(240, 200, 60, 20)))
        scene.vector("near", z=9)(lambda pen: pen.rectangle([60, 40, 70, 50], fill=(240, 90, 120, 255)))  # 在人物左边
        faint_only = {i["other"] for i in self.report(scene)["unruled_overlaps"]}
        self.assertEqual(faint_only, {"faint"})
        probe = self.mood_scene(bubbles_over_person=False)
        probe.vector("faint", z=9)(lambda pen: pen.circle((100, 60), 6, fill=(240, 200, 60, 20)))
        probe.forbid_overlap("faint", "character")
        self.assertTrue(probe.overlaps(), "同一个像素 forbid_overlap 也算压到")

    def test_large_scene_layers_are_skipped_and_listed(self):
        report = self.report(self.mood_scene())
        self.assertIn("sky", report["skipped_large_layers"], "整张的天空不提醒，但会列出来给审阅看")
        for name in ("sun", "ground"):
            self.assertNotIn(name, report["skipped_large_layers"], "没超过画面 10% 的照常检查")
        big = self.mood_scene(bubbles_over_person=False)
        big.vector("backdrop", z=1)(lambda pen: pen.rectangle([40, 0, 160, 120], fill=(100, 100, 100, 255)))
        report = self.report(big, "c7")
        self.assertIn("backdrop", report["skipped_large_layers"])
        self.assertEqual(report["warnings"], [])

    def test_two_parts_overlapping_each_other_warn(self):
        side = load_part(self.trust.add(person_image(), "side.png", pose="side"))
        scene = Scene(200, 120, scale=4)
        scene.vector("sky", z=0)(lambda pen: pen.rectangle([0, 0, 200, 120], fill=(30, 20, 60, 255)))
        scene.image("character", 10, self.person, feet=(100, 110))
        scene.image("friend", 11, side, feet=(120, 110))
        report = self.report(scene)
        self.assertEqual([(i["character"], i["other"]) for i in report["unruled_overlaps"]], [("character", "friend")])
        scene.allow_overlap("character", "friend", "挤在一起合照")
        self.assertEqual(self.report(scene, "c8")["warnings"], [])

    def test_check_json_keeps_its_old_keys(self):
        report = self.report(self.mood_scene())
        for key in ("character", "overlaps", "edge_points", "problems"):
            self.assertIn(key, report)
        self.assertEqual(self.report(self.mood_scene(bubbles_over_person=False), "c9")["warnings"], [])
        self.assertIn("warnings", self.report(self.mood_scene(bubbles_over_person=False), "c10"), "warnings 一律会有（可能是空的）")


@unittest.skipUnless(POSIX, "零件要检查拥有者与权限，只在 Linux 跑（容器或 VM）")
class PartsTest(unittest.TestCase):
    """预期洞数只能来自维护员放进信任目录的封印零件；她的捷径（自己封、改 json、改预期值、假零件）要被挡下或标红。"""

    def setUp(self):
        self.trust = TrustedParts()
        self.cut = cut_out(figure(), background_seeds=[(125, 65)])
        self.png = self.trust.add(self.cut.image, "front.png")
        self.tmp = self.trust.root

    def tearDown(self):
        self.trust.close()

    def scene_with(self, character, **kwargs):
        scene = Scene(400, 300, scale=2)
        scene.vector("sky", z=0)(lambda pen: pen.rectangle([0, 0, 400, 300], fill=(30, 20, 60, 255)))
        scene.image("character", 10, character, feet=(200, 290), **kwargs)
        return scene

    def test_sealed_part_carries_its_holes_and_passes(self):
        part = load_part(self.png)
        self.assertGreater(part.expected_holes, 0, "被挖掉的空隙有算进封印")
        report = self_check(self.scene_with(part), os.path.join(self.tmp, "check"))
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["character"]["expected_from"], "seal")
        self.assertNotIn("overrides", report)

    def test_her_shortcut_is_refused(self):
        """11/1 她写的是 expected_holes=character_report(whale)["hole_pixels"]。"""
        part = load_part(self.png)
        measured = character_report(part.image)["hole_pixels"]
        with self.assertRaises(TypeError) as caught:
            self.scene_with(part, expected_holes=measured)
        self.assertIn("load_part", str(caught.exception))

    def test_override_is_flagged_red_and_blocks_publish(self):
        part = load_part(self.png)
        measured = character_report(part.image)["hole_pixels"]
        scene = self.scene_with(part, holes_override=(measured, "照量到的填"))
        folder = os.path.join(self.tmp, "out")
        os.makedirs(folder)
        with self.assertRaises(CheckFailed) as caught:
            publish(scene, os.path.join(folder, "pic.png"), os.path.join(folder, "check"))
        self.assertTrue(any("被手动改成" in p for p in caught.exception.problems))
        with open(os.path.join(folder, "check", "check.json"), encoding="utf-8") as file:
            saved = json.load(file)
        self.assertEqual(saved["overrides"], [{"layer": "character", "holes_override": measured, "reason": "照量到的填"}])
        self.assertEqual(saved["character"]["expected_from"], "override")
        with self.assertRaises(TypeError):
            self.scene_with(part, holes_override=measured)  # 没有理由不行

    def test_unsealed_character_is_a_problem(self):
        raw = Image.open(self.png)
        report = self_check(self.scene_with(raw), os.path.join(self.tmp, "check2"))
        self.assertTrue(any("load_part" in p for p in report["problems"]))

    def test_self_sealed_part_in_her_own_folder_is_refused(self):
        """Rin 的复现：在自己目录里放一张有 64 个洞的 png 并自封，以前得到 expected_from=seal、problems=[]。"""
        holey = self.cut.image.copy()
        for k in range(64):
            holey.putpixel((60 + k % 8, 70 + k // 8), (0, 0, 0, 0))
        mine = os.path.join(self.tmp, "srv", "dsh", "discord", "draw", "parts")
        os.makedirs(mine)
        path = self.trust.add(holey, "front.png", folder=mine)   # 格式完全正确的封印
        with self.assertRaises(PartError) as caught:
            load_part(path)
        self.assertIn("不在", str(caught.exception))
        link = os.path.join(self.trust.folder, "mine.png")      # 从信任目录连结到她的档也不行
        os.symlink(path, link)
        with self.assertRaises(PartError):
            load_part(link)

    def test_owner_and_mode_of_every_level_are_checked(self):
        real = os.path.realpath(self.png)
        cases = [(real, 0o664), (os.path.splitext(real)[0] + ".json", 0o666),
                 (self.trust.folder, 0o775), (self.trust.parts_root, 0o777), (self.trust.top, 0o757)]
        for path, mode in cases:
            before = os.stat(path).st_mode & 0o777
            os.chmod(path, mode)
            try:
                with self.assertRaises(PartError, msg=f"{path} {mode:o}") as caught:
                    load_part(self.png)
                self.assertIn("维护员", str(caught.exception))
            finally:
                os.chmod(path, before)
        load_part(self.png)
        sidecar = os.path.splitext(real)[0] + ".json"
        os.rename(sidecar, sidecar + ".real")
        os.symlink(sidecar + ".real", sidecar)
        with self.assertRaises(PartError):
            load_part(self.png)
        os.remove(sidecar)
        os.rename(sidecar + ".real", sidecar)
        with mock.patch.object(parts, "TRUSTED_UID", os.getuid() + 1):   # 不是该拥有者的档
            with self.assertRaises(PartError):
                load_part(self.png)

    def test_bad_seal_contents_are_refused_with_a_part_error(self):
        """Rin：sidecar 改成 1,000,000,000 以前照收；坏 JSON、型别不对以前可能丢出原始例外。"""
        measured = load_part(self.png).expected_holes
        sidecar = os.path.splitext(os.path.realpath(self.png))[0] + ".json"
        with open(sidecar, encoding="utf-8") as file:
            good = file.read()
        bad = [
            dict(expected_hole_pixels=1_000_000_000), dict(expected_hole_pixels=-1), dict(expected_hole_pixels="12"),
            dict(expected_hole_pixels=12.0), dict(expected_hole_pixels=True), dict(expected_hole_pixels=None),
            dict(expected_hole_pixels=measured + 500),          # 在范围内，但跟图上量到的不一样
            dict(sha256=12), dict(sha256="ab"), dict(seal_version=1), dict(pose=None), dict(pose="../x"),
            dict(cut_edges_ok="bottom"), dict(cut_edges_ok=["middle"]), dict(cut_edges_ok=["top", "top"]),
        ]
        for change in bad:
            self.trust.rewrite_json(self.png, **change)
            with self.assertRaises(PartError, msg=str(change)) as caught:
                load_part(self.png)
            self.assertIn("维护员", str(caught.exception))
            with open(sidecar, "w", encoding="utf-8") as file:
                file.write(good)
        for text in ("{broken", "[1, 2]", "null", "\udcff", ""):
            with open(sidecar, "w", encoding="utf-8", errors="surrogateescape") as file:
                file.write(text)
            with self.assertRaises(PartError, msg=text):
                load_part(self.png)
        with open(sidecar, "wb") as file:
            file.write(b"\xff\xfe not utf-8")
        with self.assertRaises(PartError):
            load_part(self.png)
        with open(sidecar, "w", encoding="utf-8") as file:
            file.write(good)
        load_part(self.png)

    def test_tampered_or_unsealed_part_is_refused(self):
        real = os.path.realpath(self.png)
        image = Image.open(real).convert("RGBA")
        image.putpixel((80, 80), (0, 0, 0, 0))  # 在零件上多挖一个洞
        image.save(real)
        with self.assertRaises(PartError):
            load_part(self.png)
        with open(os.path.splitext(real)[0] + ".json", "w", encoding="utf-8") as file:
            json.dump({"removed_pixels": 3268}, file)  # 旧版的抠图报告，不是封印
        with self.assertRaises(PartError):
            load_part(self.png)
        os.remove(os.path.splitext(real)[0] + ".json")
        with self.assertRaises(PartError):
            load_part(self.png)

    def test_forged_or_modified_parts_are_refused(self):
        """Rin：scene.image 以前用 duck typing，任何有 image / expected_holes 的物件都收。"""
        part = load_part(self.png)
        duck = types.SimpleNamespace(image=part.image, expected_holes=10**9, path=part.path, sha256=part.sha256)
        with self.assertRaises(TypeError):
            self.scene_with(duck)
        handmade = Part(image=part.image, expected_holes=part.expected_holes, path=part.path, sha256=part.sha256)
        with self.assertRaises(TypeError):
            self.scene_with(handmade)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            part.expected_holes = 10**9
        part.image.putpixel((80, 80), (0, 0, 0, 0))             # 读进来之后在记忆体里挖洞
        with self.assertRaises(PartError):
            self.scene_with(part)
        self.scene_with(load_part(self.png))

    def test_current_link_must_be_roots_and_point_to_a_set(self):
        load_part(self.png)
        os.unlink(self.trust.current)
        os.symlink("../../../srv", self.trust.current)               # 指到别处
        with self.assertRaises(PartError):
            load_part(self.png)
        os.unlink(self.trust.current)
        os.symlink(self.trust.SET, self.trust.current)
        if os.geteuid() == 0:
            os.lchown(self.trust.current, 65534, 65534)             # 不是维护帐号建的连结
            with self.assertRaises(PartError) as caught:
                load_part(self.png)
            self.assertIn("连结", str(caught.exception))
            os.lchown(self.trust.current, 0, 0)
        load_part(self.png)
        loose = os.path.join(self.trust.parts_root, "loose.png")    # 零件总目录里、不在任何一组
        Image.open(os.path.realpath(self.png)).save(loose)
        with self.assertRaises(PartError):
            load_part(loose)

    def test_part_keyword_and_cut_edges_from_the_seal(self):
        bust = self.cut.image.crop((0, 0, 160, 100))                 # 故意裁掉下半身的半身特写
        plain = load_part(self.trust.add(bust, "bust-plain.png", pose="bust"))
        sealed = load_part(self.trust.add(bust, "bust.png", pose="bust", cut_edges_ok=("bottom",)))
        self.assertEqual(sealed.cut_edges_ok, ("bottom",))
        scene = Scene(400, 300, scale=2)
        scene.vector("sky", z=0)(lambda pen: pen.rectangle([0, 0, 400, 300], fill=(30, 20, 60, 255)))
        scene.image("character", 10, part=plain, feet=(200, 290))
        report = self_check(scene, os.path.join(self.tmp, "c1"))
        self.assertTrue(any("裁切框" in p for p in report["problems"]), "没写明的裁切边要报（以前补了透明边就量不到）")
        scene.image("character", 10, part=sealed, feet=(200, 290))
        report = self_check(scene, os.path.join(self.tmp, "c2"))
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["character"]["cut_edges_ok"], ["bottom"])
        with self.assertRaises(TypeError):
            scene.image("x", 1, sealed, part=sealed)

    def test_large_loose_image_layer_is_a_problem(self):
        scene = self.scene_with(load_part(self.png))
        big = Image.open(os.path.realpath(self.png)).convert("RGBA").resize((300, 300))
        scene.image("candidate-whale", 5, big, top_left=(0, 0))
        report = self_check(scene, os.path.join(self.tmp, "c3"))
        self.assertTrue(any("candidate-whale" in p and "不是封印零件" in p for p in report["problems"]))
        small = self.scene_with(load_part(self.png))
        small.image("sticker", 5, big.resize((40, 40)), top_left=(0, 0))
        self.assertEqual(self_check(small, os.path.join(self.tmp, "c4"))["problems"], [], "小道具图不受影响")

    def test_seal_tool_is_not_in_the_package(self):
        for name in ("seal", "save_part", "_main"):
            self.assertFalse(hasattr(parts, name), name)
        with open(os.path.join(HERE, "install_whaledraw.sh"), encoding="utf-8") if os.path.exists(
                os.path.join(HERE, "install_whaledraw.sh")) else open(os.devnull) as file:
            self.assertNotIn("seal_part", file.read(), "封印工具不装进安装目录")

    @unittest.skipUnless(os.path.exists(os.path.join(HERE, "seal_part.py")), "【未跑】seal_part.py 不在这里（安装目录本来就不放）")
    def test_seal_tool_refuses_non_root(self):
        if os.geteuid() == 0:
            self.skipTest("以 root 执行时这一条不适用（容器里另外以 dsh 身分跑）")
        result = subprocess.run([sys.executable, "-I", os.path.join(HERE, "seal_part.py"), "/opt/dsh/whaledraw-x", "requests"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("root", result.stderr)

    def test_api_cheat_sheet_example_runs(self):
        """API.md 的最小例子要真的能跑（只把零件路径换成测试用的）。"""
        with open(os.path.join(HERE, "API.md"), encoding="utf-8") as file:
            text = file.read()
        code = text.split("```python", 1)[1].split("```", 1)[0]
        self.assertIn("/opt/dsh/whaledraw-parts/current/whale_front.png", code)
        code = code.replace("/opt/dsh/whaledraw-parts/current/whale_front.png", self.png)
        code = code.replace("os.path.dirname(os.path.abspath(__file__))", repr(self.tmp))
        namespace = {}
        exec(compile(code, "API.md", "exec"), namespace)  # noqa: S102 - our own document
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "out.png")), "例子照写就能出图")


@unittest.skipUnless(POSIX, "零件要检查拥有者与权限，只在 Linux 跑（容器或 VM）")
class HalloweenSyntheticTest(unittest.TestCase):
    """不靠任何本机档案：用假人物跑完整的万圣节场景，验证摆位、重叠、自检、出图这一整条。"""

    def test_full_scene_with_synthetic_character(self):
        import halloween
        trust = TrustedParts()
        try:
            png = trust.add(cut_out(figure(), background_seeds=[(125, 65)]).image, "front.png")
            folder = os.path.join(trust.root, "out")
            final, check = halloween.render_to(load_part(png), folder)
            self.assertEqual(final.size, (1200, 800))
            self.assertEqual(check["problems"], [])
            # 假人物比真的宽，会碰到树等别的层，所以这里只看：影子那一条有声明、提醒只是提醒不挡出图。
            self.assertEqual([i["other"] for i in check["allowed_overlaps"]], ["shadows"])
            self.assertNotIn("shadows", " ".join(check["warnings"]))
            self.assertTrue(os.path.exists(os.path.join(folder, "halloween.png")))
        finally:
            trust.close()

    def run_entry(self, trust, folder):
        """跑真正的入口 halloween.main，回传它印出来的那行 JSON（和有没有丢例外）。"""
        import halloween
        png = trust.add(cut_out(figure(), background_seeds=[(125, 65)]).image, "front.png")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            halloween.main(png, folder)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def test_entry_prints_warnings_on_a_successful_picture(self):
        """Rin 0.2.2：publish 有把 warnings 写进 check.json，但入口成功时只印 ok/problems，提醒看不到。"""
        trust = TrustedParts()
        try:
            printed = self.run_entry(trust, os.path.join(trust.root, "out"))
            self.assertTrue(printed["ok"])
            self.assertEqual(printed["problems"], [])
            self.assertTrue(printed["warnings"], "入口输出里一定看得到 warnings")
            self.assertTrue(all("没有为这一对设规则" in w for w in printed["warnings"]), printed["warnings"])
            self.assertEqual([i["other"] for i in printed["allowed_overlaps"]], ["shadows"], "有声明就印出来给人看理由")
            self.assertIn("ground", printed["skipped_large_layers"])
            with open(os.path.join(trust.root, "out", "check", "check.json"), encoding="utf-8") as file:
                saved = json.load(file)
            self.assertEqual(printed["warnings"], saved["warnings"], "入口印的跟 check.json 一致")
        finally:
            trust.close()

    def test_entry_prints_warnings_when_the_picture_is_rejected_too(self):
        import halloween
        from whaledraw import check as check_module
        trust = TrustedParts()
        try:
            real = check_module.self_check

            def failing(scene, folder, character="character", rules_layers=None):
                report = real(scene, folder, character, rules_layers)
                report["problems"].append("测试用：故意没过")
                return report
            out = io.StringIO()
            png = trust.add(cut_out(figure(), background_seeds=[(125, 65)]).image, "front.png")
            with mock.patch.object(check_module, "self_check", failing), mock.patch.object(halloween, "publish", check_module.publish):
                with contextlib.redirect_stdout(out), self.assertRaises(CheckFailed):
                    halloween.main(png, os.path.join(trust.root, "out2"))
            printed = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertFalse(printed["ok"])
            self.assertEqual(printed["problems"], ["测试用：故意没过"])
            self.assertTrue(printed["warnings"], "没过的时候也印提醒")
        finally:
            trust.close()

    def test_summarize_prints_the_optional_keys_only_when_they_have_content(self):
        from whaledraw.check import summarize
        self.assertEqual(summarize({"problems": [], "warnings": []}), {"problems": [], "warnings": []})
        self.assertEqual(summarize({"problems": []}), {"problems": [], "warnings": []}, "旧格式的报告也不会出错")
        full = summarize({"problems": ["p"], "warnings": ["w"], "allowed_overlaps": [{"x": 1}], "skipped_large_layers": ["sky"],
                          "unruled_overlaps": [{"y": 2}]})
        self.assertEqual(sorted(full), ["allowed_overlaps", "problems", "skipped_large_layers", "warnings"])

    def test_docs_tell_her_to_look_at_warnings_after_success(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "API.md"), encoding="utf-8") as file:
            api = file.read()
        self.assertIn("whaledraw-0.2.2", api)
        self.assertNotIn("whaledraw-0.2.1", api)
        self.assertIn("出图成功也要看提醒", api)
        self.assertIn("理由只是写给维护员审阅的声明，机器不会验证它", api)
        self.assertIn('allow_overlap("*", "character"', api)
        self.assertIn("不管是什么", api)
        persona = os.path.join(here, "..", "social", "persona-group.md")
        if not os.path.exists(persona):
            self.skipTest("persona-group.md 不在这个资料夹（容器只放了 draw/）；本机的完整 repo 里会跑")
        with open(persona, encoding="utf-8") as file:
            text = file.read()
        self.assertIn("就算 `publish` 成功出圖了", text)
        self.assertIn("check/check-overlap.png", text)
        self.assertIn("skipped_large_layers", text)
        self.assertIn("不只天空、地面，任何大圖層都一樣", text)
        self.assertNotIn("whaledraw-0.2.1", text)


@unittest.skipUnless(POSIX and os.path.exists(SAMPLE_SHEET),
                     "【未跑】真实样本测试：要在 Linux 上、并找到 " + SAMPLE_SHEET + "；设 WHALEDRAW_SAMPLES 指到有 whale_sheet.png 的资料夹")
class HalloweenSampleTest(unittest.TestCase):
    def test_redraw_passes_self_check(self):
        import halloween
        trust = TrustedParts()
        try:
            cut = halloween.cut_front(SAMPLE_SHEET, os.path.join(trust.root, "cut", "whale_front.png"))
            png = trust.add(cut.image, "whale_front.png")        # 维护员封印这一步
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                final, check = halloween.main(png, os.path.join(trust.root, "out"))
            printed = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertEqual((printed["ok"], printed["problems"], printed["warnings"]), (True, [], []),
                             "没有提醒时入口也印 warnings（空的）")
            self.assertEqual([i["other"] for i in printed["allowed_overlaps"]], ["shadows"])
            self.assertEqual(final.size, (1200, 800))
            self.assertEqual(check["problems"], [])
            self.assertEqual(check["warnings"], [], "真的立绘：万圣节图所有重叠都有规则或声明")
            self.assertLess(check["character"]["halo"], 0.02)
        finally:
            trust.close()


def opaque_sheet():
    """一张「素材」：白底、不透明的假设定图（跟 figure() 同一个人物，放在较大的画布上）。"""
    sheet = Image.new("RGB", (400, 260), (254, 254, 254))
    sheet.paste(figure(), (20, 30))
    return sheet


class CandidateEnv:
    """候选零件的测试环境：信任零件目录＋素材目录（维护员的）＋她的候选与申请目录。"""

    def __init__(self):
        from whaledraw import candidate
        self.c = candidate
        self.trust = TrustedParts()
        self.sources = os.path.join(self.trust.top, "whaledraw-sources")
        os.makedirs(self.sources)
        os.chmod(self.sources, 0o755)
        opaque_sheet().save(os.path.join(self.sources, "sheet.png"))
        os.chmod(os.path.join(self.sources, "sheet.png"), 0o644)
        draw = os.path.join(self.trust.root, "srv", "dsh", "discord", "draw")
        self.lock = os.path.join(self.trust.parts_root, "requests.lock")
        with open(self.lock, "w"):
            pass
        os.chmod(self.lock, 0o644)                       # 维护员建的共用锁：root 所有、0644
        self.patch = mock.patch.multiple(candidate, SOURCES=self.sources, CANDIDATES=os.path.join(draw, "candidates"),
                                         REQUESTS=os.path.join(draw, "requests"),
                                         DECISIONS=os.path.join(self.trust.parts_root, "decisions.log"),
                                         SHARED_LOCK=self.lock)
        self.patch.start()

    def close(self):
        self.patch.stop()
        self.trust.close()

    def hair_gap(self, info):
        """编号图上「发丝圈里的空隙」那一块（假人物在 (145, 95) 附近）。"""
        return min(info["enclosed"], key=lambda h: (h["center"][0] - 125) ** 2 + (h["center"][1] - 65) ** 2)["id"]

    def make(self, name, submit=True):
        info = self.c.prepare(name, "sheet.png", [20, 30, 180, 190])
        report = self.c.finalize(name, [self.hair_gap(info)])
        if submit:
            return self.c.submit(name, "side", "測試用"), report
        return None, report


@unittest.skipUnless(POSIX, "要检查拥有者与权限，只在 Linux 跑")
class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.env = CandidateEnv()
        self.c = self.env.c

    def tearDown(self):
        self.env.close()

    def test_prepare_finalize_submit(self):
        info = self.c.prepare("side", "sheet.png", [20, 30, 180, 190])
        folder = os.path.join(self.c.CANDIDATES, "side")
        for name in ("enclosed.png", "preview.png", "prepare.json"):
            self.assertTrue(os.path.exists(os.path.join(folder, name)), name)
        self.assertGreaterEqual(len(info["enclosed"]), 2, "围裙和发丝圈都列出来")
        gap = self.env.hair_gap(info)
        report = self.c.finalize("side", [gap])
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["remove_ids"], [gap])
        for name in ("candidate.png", "check-removed.png", "check-edges.png", "report.json"):
            self.assertTrue(os.path.exists(os.path.join(folder, name)), name)
        request = self.c.submit("side", "side-standing", "侧面站姿")
        self.assertEqual(request["say"], "我做好了新姿勢候選『side-standing』，等維護員封印")
        self.assertTrue(os.path.exists(os.path.join(self.c.REQUESTS, request["request"] + ".json")))
        self.assertEqual(self.c.status()[0]["state"], "waiting for the maintainer")

    def test_candidate_is_never_loadable(self):
        self.env.make("side", submit=False)
        png = os.path.join(self.c.CANDIDATES, "side", "candidate.png")
        with self.assertRaises(PartError):
            load_part(png)
        copy = self.env.trust.add(Image.open(png), "copied.png", folder=os.path.join(self.c.CANDIDATES, "side"))
        with self.assertRaises(PartError):                               # 自己配一份格式正确的封印也不行
            load_part(copy)
        link = os.path.join(self.c.CANDIDATES, "link.png")
        os.symlink(png, link)
        with self.assertRaises(PartError):
            load_part(link)

    def test_no_resubmit_no_edit_after_submit_and_pending_cap(self):
        self.env.make("side")
        with self.assertRaises(self.c.CandidateError):
            self.c.submit("side", "side", "再送一次")
        with self.assertRaises(self.c.CandidateError):
            self.c.finalize("side", [])
        with self.assertRaises(self.c.CandidateError):
            self.c.prepare("side", "sheet.png", [20, 30, 180, 190])
        self.env.make("back")
        self.env.make("bust")
        self.env.make("extra", submit=False)
        with self.assertRaises(self.c.CandidateError) as caught:
            self.c.submit("extra", "extra", "第四份")
        self.assertIn("上限", str(caught.exception))
        self.c.withdraw("bust")
        self.c.submit("extra", "extra", "撤回一份之后可以交")
        self.c.finalize("bust", [])                                       # 撤回之后才能再改

    def test_sources_only_from_the_maintainers_folder(self):
        mine = os.path.join(self.env.trust.root, "mine.png")
        opaque_sheet().save(mine)
        for source in (mine, "../mine.png", "missing.png", ".hidden.png"):
            with self.assertRaises(self.c.CandidateError, msg=source):
                self.c.prepare("side", source, [20, 30, 180, 190])
        os.symlink(mine, os.path.join(self.env.sources, "linked.png"))
        with self.assertRaises(self.c.CandidateError):
            self.c.prepare("side", "linked.png", [20, 30, 180, 190])
        os.chmod(os.path.join(self.env.sources, "sheet.png"), 0o666)
        with self.assertRaises(self.c.CandidateError):
            self.c.prepare("side", "sheet.png", [20, 30, 180, 190])

    def test_bad_crop_ids_and_names_are_refused(self):
        for crop in ([0, 0, 5000, 100], [10, 10, 5, 50], [0, 0, 10, 10], [0, 0, 100.5, 100]):
            with self.assertRaises(self.c.CandidateError, msg=str(crop)):
                self.c.prepare("side", "sheet.png", crop)
        self.c.prepare("side", "sheet.png", [20, 30, 180, 190])
        with self.assertRaises(self.c.CandidateError):
            self.c.finalize("side", [999])
        for name in ("../x", "Side", "", "a" * 50):
            with self.assertRaises(self.c.CandidateError, msg=name):
                self.c.prepare(name, "sheet.png", [20, 30, 180, 190])

    def test_cut_edges_must_be_declared(self):
        self.c.prepare("bust", "sheet.png", [20, 30, 180, 120])          # 切到身体
        report = self.c.finalize("bust", [])
        self.assertTrue(any("裁切框" in p for p in report["problems"]))
        with self.assertRaises(self.c.CandidateError):
            self.c.submit("bust", "bust", "有问题不能交")
        report = self.c.finalize("bust", [], ["bottom"])
        self.assertEqual(report["problems"], [])


@unittest.skipUnless(POSIX, "要 flock，只在 Linux 跑")
class CandidateAtomicTest(unittest.TestCase):
    """Rin 0.2.1：submit 的检查与写入不在同一个锁里，可以并发超过 3 份、或留下撤不掉的孤立申请。"""

    def setUp(self):
        self.env = CandidateEnv()
        self.c = self.env.c

    def tearDown(self):
        self.c._CRASH_AT = None
        self.env.close()

    def race(self, *calls):
        """同时执行几个呼叫（各自一条执行绪、各自开锁档），回传每个的结果或例外。"""
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, function):
            barrier.wait()
            try:
                results[index] = function()
            except Exception as error:  # noqa: BLE001
                results[index] = error

        threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        return results

    def slow_pending(self):
        """把「数待审」和「写入」之间的空档拉长，没有锁的话一定会撞。"""
        real = self.c.pending

        def slow():
            rows = real()
            time.sleep(0.3)
            return rows
        return mock.patch.object(self.c, "pending", slow)

    def test_concurrent_third_and_fourth_submit_never_exceed_the_cap(self):
        self.env.make("one")
        self.env.make("two")
        self.env.make("three", submit=False)
        self.env.make("four", submit=False)
        with self.slow_pending():
            results = self.race(lambda: self.c.submit("three", "three", "第三份"),
                                lambda: self.c.submit("four", "four", "第四份"))
        ok = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, self.c.CandidateError)]
        self.assertEqual((len(ok), len(refused)), (1, 1), results)
        self.assertIn("上限", str(refused[0]))
        self.assertEqual(len(self.c.pending()), 3)
        loser = "four" if ok[0]["name"] == "three" else "three"
        self.assertNotIn(loser, [r["name"] for r in self.c.status() if r["request"]], "输的那份没有留下锁标记")

    def test_concurrent_submits_of_the_same_name_make_one_request(self):
        """回归测试而已：把锁拿掉这一项也不一定失败（另一条执行绪的自动修复会清掉对方的暂存档与标记），
        所以它不能当作「锁有效」的证据；锁的证据是上面的上限测试和下面的确定性交错测试。"""
        self.env.make("side", submit=False)
        with self.slow_pending():
            results = self.race(lambda: self.c.submit("side", "side", "a"), lambda: self.c.submit("side", "side", "b"))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1, results)
        self.assertEqual(len(self.c.pending()), 1)

    def test_crash_during_submit_leaves_no_orphan_and_heals(self):
        for point in ("submit-after-temp", "submit-after-marker"):
            with self.subTest(point=point):
                name = "crash-" + point.split("-")[-1]
                self.env.make(name, submit=False)
                self.c._CRASH_AT = point
                with self.assertRaises(self.c.Crash):
                    self.c.submit(name, name, "当掉")
                self.c._CRASH_AT = None
                self.assertNotIn(name, [r["name"] for r in self.c.pending()], "没送出的不算待审")
                row = next(r for r in self.c.status() if r["name"] == name)
                self.assertEqual(row["state"], "finalized", "孤立的锁标记被清掉，候选可以再改")
                self.assertFalse([e for e in os.listdir(self.c.REQUESTS) if e.endswith(".part")], "暂存档清掉")
                self.c.submit(name, name, "重交")                   # 修复后可以正常重交
                self.assertIn(name, [r["name"] for r in self.c.pending()])
        self.c._CRASH_AT = "submit-after-publish"
        self.env.make("late", submit=False)
        with self.assertRaises(self.c.Crash):
            self.c.submit("late", "late", "送出后才当掉")
        self.c._CRASH_AT = None
        self.assertIn("late", [r["name"] for r in self.c.pending()], "改名之后就算送出了，状态一致")
        self.assertEqual(next(r for r in self.c.status() if r["name"] == "late")["state"], "waiting for the maintainer")

    def test_shared_lock_must_be_trustworthy(self):
        self.env.make("side", submit=False)
        lock = self.env.lock
        self.assertFalse(os.path.exists(os.path.join(self.c.REQUESTS, ".lock")), "不再用她目录里的锁档")
        os.rename(lock, lock + ".away")                                   # 锁档不见了
        with self.assertRaises(self.c.CandidateError) as caught:
            self.c.finalize("side", [])
        self.assertIn("还没建立", str(caught.exception))
        os.symlink(lock + ".away", lock)                                  # 换成连结
        with self.assertRaises(self.c.CandidateError):
            self.c.finalize("side", [])
        os.unlink(lock)
        os.rename(lock + ".away", lock)
        os.chmod(lock, 0o666)                                             # 谁都能写
        with self.assertRaises(self.c.CandidateError) as caught:
            self.c.finalize("side", [])
        self.assertIn("警告", str(caught.exception))
        os.chmod(lock, 0o644)
        if os.geteuid() == 0:
            os.chown(lock, 65534, 65534)                                  # 不是维护帐号的
            with self.assertRaises(self.c.CandidateError):
                self.c.finalize("side", [])
            os.chown(lock, 0, 0)
        self.c.finalize("side", [])

    def test_lock_replaced_while_held_fails_closed_before_publishing(self):
        self.env.make("side", submit=False)
        lock = self.env.lock

        def replace_lock(name):
            if name == "submit-after-marker":
                fresh = lock + ".new"
                with open(fresh, "w"):
                    pass
                os.chmod(fresh, 0o644)
                os.replace(fresh, lock)                                   # 拿着锁的时候，锁档被换掉
        with mock.patch.object(self.c, "_crashpoint", replace_lock):
            with self.assertRaises(self.c.CandidateError) as caught:
                self.c.submit("side", "side", "锁档被换")
        self.assertIn("被换掉", str(caught.exception))
        self.assertEqual(self.c.pending(), [], "没有送出")
        self.assertEqual(next(r for r in self.c.status() if r["name"] == "side")["state"], "finalized", "没有留下锁标记")
        with self.c._Lock() as held:
            os.replace(self.env.lock, self.env.lock + ".old")
            with open(self.env.lock, "w"):
                pass
            os.chmod(self.env.lock, 0o644)
            with self.assertRaises(self.c.CandidateError):
                held.check()

    def test_decisions_log_is_never_read_past_a_bad_line(self):
        self.env.make("side")
        with open(self.c.DECISIONS, "wb") as file:
            file.write(b'{"request": "x", "decision": "rejected"}\n{"request": "half')   # 写到一半
        with self.assertRaises(self.c.DecisionsCorrupt):
            self.c.status()
        with open(self.c.DECISIONS, "wb") as file:
            file.write(b'not json\n{"request": "x", "decision": "rejected"}\n')
        with self.assertRaises(self.c.DecisionsCorrupt):
            self.c.pending()

    def test_orphan_request_is_listed_and_can_be_withdrawn(self):
        request, _ = self.env.make("side")
        os.remove(os.path.join(self.c.CANDIDATES, "side", "submitted.json"))   # 锁标记不见了（例如旧版本中断）
        rows = self.c.status()
        self.assertTrue(any(r["request"] == request["request"] and r["state"].startswith("orphan") for r in rows), rows)
        self.c.withdraw("side")
        self.assertEqual(self.c.pending(), [])
        self.assertFalse(any(r["state"].startswith("orphan") for r in self.c.status()))

    def test_crash_during_withdraw_heals(self):
        self.env.make("side")
        self.c._CRASH_AT = "withdraw-after-rename"
        with self.assertRaises(self.c.Crash):
            self.c.withdraw("side")
        self.c._CRASH_AT = None
        self.assertEqual(self.c.pending(), [])
        self.c.finalize("side", [])                                     # 孤立的锁标记被清掉
        self.c.submit("side", "side", "撤回后重交")


def load_seal_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("seal_part", os.path.join(HERE, "seal_part.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(POSIX and os.path.exists(os.path.join(HERE, "seal_part.py")) and hasattr(os, "geteuid")
                     and os.geteuid() == 0, "【未跑】封印工具测试要以 root 在 Linux 跑，而且要有 seal_part.py（安装目录不放）")
class SealToolTest(unittest.TestCase):
    def setUp(self):
        self.env = CandidateEnv()
        self.c = self.env.c
        self.front = self.env.trust.add(cut_out(figure(), background_seeds=[(125, 65)]).image, "whale_front.png")
        self.seal = load_seal_tool()
        self.review_patch = mock.patch.object(self.seal, "REVIEW_ROOT", os.path.join(self.env.trust.root, "review"))
        self.review_patch.start()
        self.tool = self.seal.Tool(HERE)

    def tearDown(self):
        self.review_patch.stop()
        self.env.close()

    def run_tool(self, command, **kwargs):
        args = types.SimpleNamespace(**kwargs)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            getattr(self.tool, "cmd_" + command)(args)
        return json.loads(out.getvalue())

    def test_review_seal_switches_current_and_keeps_old_parts(self):
        request, _ = self.env.make("side")
        listed = self.run_tool("requests")
        self.assertEqual([r["request"] for r in listed], [request["request"]])
        review = self.run_tool("review", request=request["request"])
        self.assertTrue(review["candidate_matches_rederived"])
        self.assertTrue(os.path.exists(os.path.join(review["look_at"], "removed-and-result.png")))
        sealed = self.run_tool("seal", request=request["request"], as_name="whale_side.png")
        new_set = os.path.basename(sealed["set"])
        self.assertNotEqual(new_set, self.env.trust.SET)
        self.assertEqual(os.readlink(self.env.trust.current), new_set)
        self.assertTrue(os.path.isdir(self.env.trust.folder), "旧的一组留着，回滚就是把 current 指回去")
        side = load_part(os.path.join(self.env.trust.current, "whale_side.png"))
        self.assertEqual(side.pose, "side")
        load_part(os.path.join(self.env.trust.current, "whale_front.png"))   # 旧零件带过来了
        self.assertEqual(self.c.pending(), [])
        self.assertEqual(self.c.status()[0]["state"], "sealed")
        index = open(os.path.join(self.env.trust.current, "INDEX.md"), encoding="utf-8").read()
        self.assertIn("whale_side.png", index)
        with self.assertRaises(SystemExit):
            self.run_tool("seal", request=request["request"], as_name="again.png")   # 处理过的不能再封
        with self.assertRaises(self.c.CandidateError) as caught:
            self.c.prepare("side", "sheet.png", [20, 30, 180, 190])          # 已封印的名字不能再用
        self.assertIn("换个名字", str(caught.exception))

    def test_candidate_changed_after_submit_is_refused(self):
        request, _ = self.env.make("side")
        png = os.path.join(self.c.CANDIDATES, "side", "candidate.png")
        image = Image.open(png).convert("RGBA")
        image.putpixel((80, 80), (0, 0, 0, 0))                           # 交了申请之后偷改
        image.save(png)
        for command, extra in (("review", {}), ("seal", {"as_name": "whale_side.png"})):
            with self.assertRaises(SystemExit, msg=command):
                self.run_tool(command, request=request["request"], **extra)
        self.assertEqual(os.readlink(self.env.trust.current), self.env.trust.SET, "没有切换")

    def test_reject_is_logged_and_visible_to_her(self):
        request, _ = self.env.make("side")
        self.run_tool("reject", request=request["request"], reason="围裙被挖掉了")
        self.assertEqual(self.c.pending(), [])
        self.assertIn("围裙被挖掉了", self.c.status()[0]["state"])
        self.c.finalize("side", [])                                     # 退回后自动解锁，照理由改
        again = self.c.submit("side", "side", "改好了")
        self.assertEqual([r["request"] for r in self.c.pending()], [again["request"]])

    def test_links_planted_in_her_folders_or_the_parts_root_are_not_followed(self):
        request, _ = self.env.make("side")
        os.symlink("/etc/hostname", os.path.join(self.c.REQUESTS, "20990101T000000Z-evil.json"))
        self.assertEqual([r["request"] for r in self.c.pending()], [request["request"]])
        with self.assertRaises(SystemExit):
            self.tool.request("20990101T000000Z-evil")
        day = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y%m%d")
        planted = os.path.join(self.env.trust.parts_root, f"{day}-1")
        if not os.path.lexists(planted):
            os.symlink(os.path.join(self.env.trust.root, "srv"), planted)   # 预先放好的连结
        sealed = self.run_tool("seal", request=request["request"], as_name="whale_side.png")
        self.assertNotEqual(os.path.realpath(sealed["set"]), os.path.realpath(planted))
        self.assertFalse(os.path.islink(sealed["set"]))

    def test_two_concurrent_seals_one_is_refused_and_no_part_is_lost(self):
        first, _ = self.env.make("side")
        second, _ = self.env.make("back")
        slow = self.seal.Tool(HERE)
        real = slow.rederive

        def rederive(req):
            time.sleep(1.0)                                              # 拿着锁慢慢做
            return real(req)
        slow.rederive = rederive
        outcome = {}

        def run_slow():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    slow.cmd_seal(types.SimpleNamespace(request=first["request"], as_name="whale_side.png"))
                outcome["slow"] = "sealed"
            except SystemExit as error:
                outcome["slow"] = f"refused {error}"

        thread = threading.Thread(target=run_slow)
        thread.start()
        time.sleep(0.3)
        with self.assertRaises(SystemExit):                              # 第二个拿不到锁就拒绝
            self.run_tool("seal", request=second["request"], as_name="whale_back.png")
        thread.join(60)
        self.assertEqual(outcome["slow"], "sealed")
        self.run_tool("seal", request=second["request"], as_name="whale_back.png")   # 之后再封
        for name in ("whale_front.png", "whale_side.png", "whale_back.png"):
            load_part(os.path.join(self.env.trust.current, name))         # 一个都没少

    def test_crash_at_every_commit_step_recovers_to_a_consistent_state(self):
        log = os.path.join(self.env.trust.parts_root, "decisions.log")

        def decided(request):
            if not os.path.exists(log):
                return []
            with open(log, encoding="utf-8") as file:
                return [r for r in map(json.loads, file) if r["request"] == request]
        journal = os.path.join(self.env.trust.parts_root, ".pending-commit.json")
        for point in ("seal-after-intent", "seal-after-build", "seal-after-switch", "seal-after-decision"):
            with self.subTest(point=point):
                name = point.split("-", 1)[1].replace("-", "")
                request, _ = self.env.make(name)
                before = os.readlink(self.env.trust.current)
                parts_before = sorted(e for e in os.listdir(self.env.trust.current) if e.endswith(".png"))
                crashing = self.seal.Tool(HERE)
                crashing.crash_at = point
                with self.assertRaises(self.c.Crash), contextlib.redirect_stdout(io.StringIO()):
                    crashing.cmd_seal(types.SimpleNamespace(request=request["request"], as_name=f"whale_{name}.png"))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.run_tool("recover")
                self.assertFalse(os.path.exists(journal), "意图档处理掉了")
                after = os.readlink(self.env.trust.current)
                parts_now = sorted(e for e in os.listdir(self.env.trust.current) if e.endswith(".png"))
                if point in ("seal-after-intent", "seal-after-build"):
                    self.assertEqual(after, before, "还没切换就当掉：current 不变")
                    self.assertEqual(parts_now, parts_before)
                    self.assertIn(request["request"], [r["request"] for r in self.c.pending()], "申请仍是待审")
                    self.assertEqual(decided(request["request"]), [])
                    leftovers = [d for d in os.listdir(self.env.trust.parts_root)
                                 if parts.SET_NAME.match(d) and d > before]
                    self.assertEqual(leftovers, [], "做到一半的那一组删掉了")
                    self.run_tool("seal", request=request["request"], as_name=f"whale_{name}.png")   # 可以重封
                else:
                    self.assertNotEqual(after, before, "切换之后才当掉：新的一组已启用")
                    self.assertEqual(len(decided(request["request"])), 1, "决定恰好一笔（补记、不重复）")
                    self.assertNotIn(request["request"], [r["request"] for r in self.c.pending()])
                    self.assertEqual(sorted(parts_now), sorted(parts_before + [f"whale_{name}.png"]), "旧零件一个都没少")
                for entry in os.listdir(self.env.trust.current):
                    if entry.endswith(".png"):
                        load_part(os.path.join(self.env.trust.current, entry))

    def test_seal_and_withdraw_never_interleave(self):
        """确定性的交错：她拿着共用锁时，维护员的 seal 拿不到（拒绝、不动任何东西）；反过来也一样。"""
        request, _ = self.env.make("side")
        before = os.readlink(self.env.trust.current)
        with mock.patch.object(self.c, "LOCK_WAIT", 0.5):
            with self.c._Lock():                                          # 她正在 withdraw（拿着锁）
                with self.assertRaises(self.c.CandidateError):
                    self.run_tool("seal", request=request["request"], as_name="whale_side.png")
            self.assertEqual(os.readlink(self.env.trust.current), before)
            slow = self.seal.Tool(HERE)
            real = slow.rederive
            started = threading.Event()

            def rederive(req):
                started.set()
                time.sleep(1.5)
                return real(req)
            slow.rederive = rederive
            thread = threading.Thread(target=lambda: slow.cmd_seal(
                types.SimpleNamespace(request=request["request"], as_name="whale_side.png")))
            with contextlib.redirect_stdout(io.StringIO()):
                thread.start()
                started.wait(10)
                with self.assertRaises(self.c.CandidateError):            # 维护员拿着锁时，她撤不了
                    self.c.withdraw("side")
                thread.join(60)
        load_part(os.path.join(self.env.trust.current, "whale_side.png"))
        with self.assertRaises(self.c.CandidateError) as caught:          # 封印之后就不能撤回
            self.c.withdraw("side")
        self.assertIn("已经封印", str(caught.exception))

    def test_recover_does_not_trust_a_dangling_or_broken_current(self):
        journal = os.path.join(self.env.trust.parts_root, ".pending-commit.json")
        good = os.readlink(self.env.trust.current)

        def intent(set_name):
            with open(journal, "w", encoding="utf-8") as file:
                json.dump({"set": set_name, "request": "r-x", "decision": "sealed", "file": "whale_x.png",
                           "previous": good}, file)

        intent("20991231-1")
        os.unlink(self.env.trust.current)
        os.symlink("20991231-1", self.env.trust.current)                 # 悬空
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_tool("recover")
        self.assertTrue(os.path.exists(journal), "意图档留着，等人处理")
        self.assertNotIn("r-x", self.c._decided())
        broken = os.path.join(self.env.trust.parts_root, "20991231-2")   # 存在但零件被改过
        shutil.copytree(os.path.join(self.env.trust.parts_root, good), broken)
        with open(os.path.join(broken, "whale_front.png"), "ab") as file:
            file.write(b"x")
        intent("20991231-2")
        os.unlink(self.env.trust.current)
        os.symlink("20991231-2", self.env.trust.current)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_tool("recover")
        self.assertTrue(os.path.exists(journal))
        self.assertNotIn("r-x", self.c._decided())
        os.unlink(self.env.trust.current)                                  # 人处理：指回去、删意图档
        os.symlink(good, self.env.trust.current)
        os.unlink(journal)
        self.run_tool("recover")

    def test_recover_stops_when_current_is_missing_or_points_elsewhere(self):
        """Rin 第 3 轮：current 不存在、或指到既不是新组也不是原本那一组的第三个组，都不能当成「切换前中断」去删组。"""
        root = self.env.trust.parts_root
        journal = os.path.join(root, ".pending-commit.json")
        good = os.readlink(self.env.trust.current)
        new_set = os.path.join(root, "20991231-5")                        # 意图里的新组（已经建好）
        shutil.copytree(os.path.join(root, good), new_set)
        third = os.path.join(root, "20991231-6")                          # 第三个组
        shutil.copytree(os.path.join(root, good), third)
        with open(journal, "w", encoding="utf-8") as file:
            json.dump({"set": "20991231-5", "request": "r-y", "decision": "sealed", "file": "whale_front.png",
                       "previous": good}, file)

        def refused():
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.run_tool("recover")
            self.assertTrue(os.path.exists(journal), "意图档留着")
            self.assertTrue(os.path.isdir(new_set), "新组没被删")
            self.assertTrue(os.path.isdir(third), "第三个组没被删")
            self.assertNotIn("r-y", self.c._decided(), "没有补记决定")

        os.unlink(self.env.trust.current)                                 # current 不见了
        refused()
        os.symlink("20991231-6", self.env.trust.current)                  # current 指到第三个组
        refused()
        os.unlink(self.env.trust.current)
        os.symlink(good, self.env.trust.current)                          # 指回原本那一组：正常回滚
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.run_tool("recover")
        self.assertIn("切换 current 之前", result["recovered"])
        self.assertFalse(os.path.exists(new_set), "回滚删掉做到一半的新组")
        self.assertTrue(os.path.isdir(third), "无关的组不动")
        self.assertFalse(os.path.exists(journal))

    def test_recover_refuses_malformed_intents_without_touching_anything(self):
        root = self.env.trust.parts_root
        journal = os.path.join(root, ".pending-commit.json")
        good = os.readlink(self.env.trust.current)
        victim = os.path.join(root, "20991231-7")
        shutil.copytree(os.path.join(root, good), victim)
        base = {"set": "20991231-7", "request": "r-z", "decision": "sealed", "file": "whale_z.png", "previous": good}
        bad = [
            {k: v for k, v in base.items() if k != "previous"},              # 缺栏位
            {k: v for k, v in base.items() if k != "request"},
            dict(base, extra=1),                                              # 多栏位
            dict(base, set=20991231),                                         # 型别错
            dict(base, set="../../srv"), dict(base, set="20991231-7/../.."), dict(base, set="current"),
            dict(base, previous="../x"), dict(base, previous=7), dict(base, previous="20991231-7"),
            dict(base, file="../whale.png"), dict(base, file="whale.txt"), dict(base, file=None),
            dict(base, decision="deleted"), dict(base, request=""), dict(base, request="a/b"), dict(base, request=5),
        ]
        for intent in bad + ["not an object", [1, 2]]:
            with self.subTest(intent=intent):
                with open(journal, "w", encoding="utf-8") as file:
                    json.dump(intent, file)
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    self.run_tool("recover")
                self.assertTrue(os.path.exists(journal), "意图档留着")
                self.assertTrue(os.path.isdir(victim), "没有删任何组")
                self.assertEqual(os.readlink(self.env.trust.current), good, "current 没动")
        with open(journal, "w", encoding="utf-8") as file:
            file.write("{broken")                                           # 根本不是 JSON
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_tool("recover")
        self.assertTrue(os.path.isdir(victim))
        os.unlink(journal)

    def test_corrupt_log_backups_have_a_cap_and_never_get_deleted_silently(self):
        log = self.c.DECISIONS
        root = self.env.trust.parts_root
        with mock.patch.object(self.seal, "MAX_CORRUPT_BACKUPS", 2):
            for n in range(3):
                with open(log, "ab") as file:
                    file.write(b'{"request": "half-%d' % n)
                if n < 2:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.run_tool("recover")
                else:
                    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
                        self.run_tool("recover")
                    self.assertIn("警告", err.getvalue())
        backups = [e for e in os.listdir(root) if e.startswith("decisions.log.corrupt-")]
        self.assertEqual(len(backups), 2, "旧备份一份都没删")

    def test_truncated_decisions_log_is_repaired_before_appending(self):
        first, _ = self.env.make("side")
        self.run_tool("seal", request=first["request"], as_name="whale_side.png")
        log = self.c.DECISIONS
        with open(log, "ab") as file:
            file.write(b'{"request": "half-writ')                         # 上次写到一半就当掉
        with self.assertRaises(self.c.DecisionsCorrupt):
            self.c.status()                                                # 她那边：拒绝，不吞坏行
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = self.run_tool("recover")
        self.assertIn("已隔离", result["log"])
        backups = [e for e in os.listdir(self.env.trust.parts_root) if e.startswith("decisions.log.corrupt-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.env.trust.parts_root, backups[0]), "rb") as file:
            self.assertTrue(file.read().endswith(b'"half-writ'), "坏的那一段完整保留在备份里")
        self.assertIn(first["request"], self.c._decided(), "前面的决定都还在")
        second, _ = self.env.make("back")
        self.run_tool("seal", request=second["request"], as_name="whale_back.png")
        self.assertIn(second["request"], self.c._decided(), "修复后新写的决定读得到")
        with open(log, "rb") as file:
            data = file.read()
        with open(log, "wb") as file:
            file.write(b"garbage\n" + data)                               # 中间（第一行）坏了：不自动修
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_tool("recover")
        with open(log, "rb") as file:
            self.assertEqual(file.read(), b"garbage\n" + data, "没有动")

    def test_seal_after_she_withdrew_is_refused(self):
        request, _ = self.env.make("side")
        self.c.withdraw("side")
        with self.assertRaises(SystemExit):
            self.run_tool("seal", request=request["request"], as_name="whale_side.png")

    def test_import_for_maintainer_parts(self):
        png = os.path.join(self.env.trust.root, "bust.png")
        cut_out(figure(), crop=(0, 0, 160, 100)).image.save(png)
        result = self.run_tool("import", png=png, as_name="whale_bust.png", pose="bust", cut_edges="bottom")
        part = load_part(result["sealed"])
        self.assertEqual(part.cut_edges_ok, ("bottom",))


if __name__ == "__main__":
    unittest.main()
