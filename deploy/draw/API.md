# whaledraw 速查（先看这份，不用读原始码）

执行：`PYTHONPATH=/opt/dsh/whaledraw-0.2.2/lib:/opt/dsh/whaledraw-0.2.2 python3 draw.py`

零件：`ls /opt/dsh/whaledraw-parts/current/`（`INDEX.md` 列出有哪些姿势）。

每张图开一个资料夹 `/srv/dsh/discord/draw/<图名>/`，写一支 `draw.py`。改哪里就改那一层的函式，整张重跑，不要在成品上改像素。

## 最小例子

```python
import os
from whaledraw.scene import Scene
from whaledraw.parts import load_part
from whaledraw.props import pumpkin, tomb, candy, dead_tree, CANDY_SIZE
from whaledraw.check import publish, CheckFailed

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 1200, 800
scene = Scene(W, H)                                   # 形状自动 4 倍超采样，边缘平滑

me = load_part("/opt/dsh/whaledraw-parts/current/whale_front.png")   # 你的立绘（封印过）
scene.image("character", 20, me, feet=(600, 772), height=560, tint=((150, 136, 210), 0.22))

@scene.vector("sky", z=0)                             # 一层一个函式；座标写最终尺寸
def sky(pen):
    pen.rectangle([0, 0, W, H], fill=(30, 20, 66, 255))

for n, (x, base) in enumerate([(160, 560), (1060, 560)], start=1):   # 会彼此靠近的东西：一个一层
    scene.vector(f"tomb-{n}", z=5)(lambda pen, x=x, base=base: tomb(pen, x, base, 48, 62))

x, y = scene.free_spot(CANDY_SIZE, near=(420, 760), avoid=["character", "tomb-1", "tomb-2"], margin=6)
@scene.vector("candy", z=21)
def candies(pen):
    candy(pen, x + CANDY_SIZE[0] / 2, y + CANDY_SIZE[1] / 2, (238, 92, 124))

scene.forbid_overlap("candy", "character")            # 压到就不出图
scene.forbid_overlap("candy", "tomb-*")               # 名字可用 *
scene.forbid_overlap_within("tomb-*")                 # 墓碑彼此不准叠

try:
    final, report = publish(scene, os.path.join(HERE, "out.png"), os.path.join(HERE, "check"))
    print("OK", report["problems"])
    for note in report["warnings"]:                   # 提醒不挡出图，但出图成功也要看；怎么消掉见下面「规则」
        print("提醒", note)
    print("allowed_overlaps", report.get("allowed_overlaps", []), "skipped_large_layers", report.get("skipped_large_layers", []))
except CheckFailed as e:
    print("FAIL", e.problems, e.rejected)             # 看 check/ 和 .REJECTED.png，改那一层再跑
```

## Scene

| 用法 | 说明 |
|---|---|
| `Scene(W, H)` | 画布 |
| `@scene.vector(name, z, blur=0, opacity=1)` | 装饰器，底下的函式收到 `pen`，画那一层 |
| `scene.image(name, z, 零件, feet=(x,y) / center= / top_left=, height= 或 scale=, tint=((r,g,b), 强度))` | 贴图。第三个参数直接放 `load_part()` 读的零件（也可以写 `part=零件`）。人物一定要用零件 |
| `scene.anchor(name, "feet"/"top"/"left"/"right"/"center")` | 某层的位置（脚边、头顶…） |
| `scene.free_spot((w,h), near=(x,y), avoid=[层名…], margin=6, within=(左,上,右,下), taken=[框…])` | 附近找不压东西的空位，回传左上角 |
| `scene.forbid_overlap(top, bottom, margin=0)` | 不准压到；名字可用 `*` |
| `scene.forbid_overlap_within("tomb-*")` | 同一类彼此不准叠 |
| `scene.allow_overlap(层名, 层名, "理由")` | 声明这两层「故意重叠」，消掉自检的「没设规则的重叠」提醒。理由一定要写；名字可用 `*`，但两边不能都是 `*` |
| `scene.mask(name)` | 这层看得见的范围（numpy bool） |

## Pen（座标都写最终尺寸）

`rectangle`、`rounded_rectangle(box, radius)`、`ellipse(box)`、`circle((x,y), r)`、`polygon(points)`、`line(points, width)`、`arc`、`pieslice`、`chord`、`bezier(points, width)`、`text((x,y), 字, 字号, 颜色, anchor="mm")`。颜色写 `(r, g, b, a)`，半透明会正确叠加。

## 现成道具（whaledraw.props）

`pumpkin(pen, cx, cy, r)`、`candy(pen, x, y, colour)`（外框 `CANDY_SIZE`）、`bat(pen, cx, cy, s)`、`dead_tree(pen, x, base, height, seed)`、`tomb(pen, x, base, w, h)`

## 规则（自检会挡）

- **不要自己填 `expected_holes`**：零件用 `load_part()` 读，预期洞数会自动带入。`scene.image(..., expected_holes=…)` 会直接报错。
- 零件只认维护员封印、放在 `/opt/dsh/whaledraw-parts/current/` 的。你目录里的 png（包括候选零件）、自己写的 json、自己做的 `Part` 都会被拒（`PartError` / `TypeError`）。不是零件的大张图片层（超过画面 10%）也算问题。
- 想要新姿势：用下面的候选工具做，交一次申请，等维护员封印。
- 真的要改洞数，只能写 `holes_override=(数字, "理由")`：check.json 会标出来，自检会当成问题、不出图，要站长确认。
- 规则名字要对得到图层：`forbid_overlap("candy-*", ...)` 但图层叫 `candy`，自检会报「一层都没对到」、不出图。改名字让它对上，不要删规则。一条规则一边最多对到 64 层。
- 自检没过又看不懂原因：停下来回报「需要维护员」，附上 check.json 的问题。不要重复封印、改 json、改预期值。
- 东西要离人物 6 像素以上（重叠检查很严：看得见的一点点覆盖都算）。
- **有一层跟人物重叠、却没为这一对设规则，自检会提醒**（`check.json` 的 `warnings`，`check-overlap.png` 上用橙色标出来；不挡出图）。例如泡泡画在人物下面，被头发挡住一部分。消掉的办法二选一：
  - 不准叠：`scene.forbid_overlap("character", "bubble")`，压到就不出图，把泡泡移开；
  - 故意叠：`scene.allow_overlap("character", "bubble", "泡泡故意飘在脸前")`，理由会记进 `check.json`，维护员审阅时看。
  `allow_overlap` 只消提醒，不会解除 `forbid_overlap`。**理由只是写给维护员审阅的声明，机器不会验证它**，所以要照实写；写 `scene.allow_overlap("*", "character", "...")` 会一次消掉所有跟人物重叠的层的提醒，每一对仍会各自留一笔理由在 `check.json` 的 `allowed_overlaps`，审阅时会逐笔看。只管用 `load_part` 读的人物层，跟别的层的重叠；超过画面 10% 的图层**不管是什么**（天空、地面、光芒，或任何一个大图层）都不检查重叠，名字会列在 `check.json` 的 `skipped_large_layers`，也会印在入口输出里；这些层要自己看成品确认。
- **出图成功也要看提醒**：`publish` 成功只代表没有「错误」。发图之前要看 `report["warnings"]`（`check.json` 里也有）和 `check/check-overlap.png` 上的橙色标记。
- 自检只是机器量得到的东西：提醒与自检数字都不能代替看图，出图前一定要用 `read_image` 看过成品。
- 会彼此靠近的物件（墓碑、糖果、石头）各画一层并加 `forbid_overlap_within` / `forbid_overlap`，不要叠在一起。
- 树干、柱子不要用 `rectangle`（顶端会是平的方块，像被切断）：用 `dead_tree`，或画成上窄下宽的 `polygon`。
- 出图一律用 `publish`。`CheckFailed` 时看 `check/check-overlap.png`（红色是压到的地方）、`check-edges.png`、`.REJECTED.png`，修好再跑。没过的图不要发。

## 新姿势的候选零件（python3 -m whaledraw.candidate）

在 `/srv/dsh/discord/draw` 底下执行，前面一样加 `PYTHONPATH=...`：

1. `sources`：看有哪些素材（维护员放在 `/opt/dsh/whaledraw-sources/`）。
2. `prepare <名字> --source whale_sheet.png --crop 左 上 右 下 [--shadow-box 左 上 右 下]`：裁图、抠背景。用 `read_image` 看 `candidates/<名字>/enclosed.png`，决定哪几个编号是背景（头发之间、两腿之间露出来的底色）。围裙、蕾丝、眼睛高光不要挖。
3. `finalize <名字> --remove 14,16,19`：看 `check-removed.png`（红色是挖掉的地方）和 `check-edges.png`。有问题就改编号再 finalize。
4. `submit <名字> --pose <姿势名> --note "一句说明"`：只交一次。然后在群里说它印出来的那一句。
5. 等维护员封印；`status` 看结果。被退回就照理由改了再交。封印前照常用现有的零件画。

注意：自检只量得到「明显不透明」的问题。例如裁切边是用透明度大于 128 的像素算的，很淡、但还看得见的直线断面会漏掉。所以 `check-removed.png`、`check-edges.png` 一定要自己看过；维护员审阅时也一定会看图，自检的数字不能代替人看。
