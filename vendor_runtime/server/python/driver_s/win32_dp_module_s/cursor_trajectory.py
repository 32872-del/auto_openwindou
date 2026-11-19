# -*- coding: utf-8 -*-
"""Win32 + DP 每会话轨迹与时序人格。

设计要点
--------
1. **每个会话一个固定的运动人格**（MotionProfile）
   真人的运动特征是稳定的：同一个人的速度、曲率偏好、抖动幅度、点击停留时长
   今天和明天是相关的。如果每次移动都重新随机全部参数，聚合起来是「平均人类」，
   但真人是「某一个具体的人」。行为建模类风控看到「同一账号每次运动特征都不同」
   本身就是异常。

2. **四种路径拓扑按人格化权重随机**
   只用单一贝塞尔形状的话，跑上万次形状家族是固定的，统计上能看出来。

3. **短距离与长距离分开建模**
   符合费茨定律：短距离是弹道式一下过去、几乎不弯；长距离才有明显转向修正。

4. **法向 / 切向噪声分离，且两端都衰减**
   横向抖动（手抖、方向修正）与沿路径的速度波动是两回事，幅度也不同。
   两端衰减是必须的：起点要精确接上一次位置（否则有跳变），终点要落准。
   噪声要做平滑 —— 真实手抖是低频的，逐点白噪声看起来像静电。

5. **非对称速度曲线（弹道段 + 校正段）**
   真人指向不是对称的加减速，而是快速冲过去再慢慢修正。

6. **末端微整定**
   停下时手会有一下回弹：冲过一点点再收回来。

7. **相邻点取整后必须差 ≥1 物理像素**
   RenderWidgetHostViewAura::OnMouseEvent (render_widget_host_view_aura.cc:2462-2469)
   会把 location 相同且 movement 为 0 的 mousemove 静默丢弃。
   缓动曲线的头尾天然会产生亚像素步长，不去重的话那几个点根本发不出去，
   实际缓动效果和设计不一样。
"""
import math
import random

_PATH_STYLES = ('line', 'arc', 'line_then_arc', 'overshoot_back')

#: 短于此距离用「弹道模型」：点少、几乎不弯
SHORT_DIST = 140.0

#: 每种风格的横向幅度（占移动距离的比例区间）。
#: 二次贝塞尔的最大偏离约为控制点偏移的一半，所以 arc 取到 0.125 时
#: 实际偏离约 0.06×距离 —— 与真人长距离指向的曲率上限相当。
_STYLE_AMP = {
    'line': (0.008, 0.030),
    'arc': (0.045, 0.125),
    'line_then_arc': (0.020, 0.075),
    'overshoot_back': (0.020, 0.060),
}


# --------------------------------------------------------------------------- #
# 运动人格
# --------------------------------------------------------------------------- #
class MotionProfile(object):
    """一个浏览器会话的固定运动特征。会话存活期间不变。"""

    def __init__(self, rng=None):
        r = rng or random.Random()
        self.speed = r.uniform(0.78, 1.30)          # 整体快慢
        self.curvature = r.uniform(0.65, 1.40)      # 弧度偏好
        self.tremor = r.uniform(0.60, 1.55)         # 抖动幅度
        self.overshoot_prob = r.uniform(0.30, 0.75)  # 长距离冲过头的概率
        self.settle_prob = r.uniform(0.55, 0.95)    # 末端回弹概率
        self.arc_side = r.uniform(-0.6, 0.6)        # 绕行方向偏好（有人爱从上绕）
        self.skew = r.uniform(0.68, 0.88)           # 速度曲线前倾程度
        # 时序
        self.hold_mu = r.uniform(0.058, 0.108)      # 点击按下时长
        # 小写字母的键间隔中位数；其它字符按 char_gap_factor 放大。
        # 原来是 0.085~0.155 且不分字符，折合连打 100 WPM——那是专业
        # 打字员的水平，最快的一成到 180 WPM（世界纪录 216）。
        self.gap_mu = r.uniform(0.105, 0.195)
        self.gap_sigma = r.uniform(0.30, 0.50)
        self.pause_prob = r.uniform(0.03, 0.10)     # 打字时偶发思考停顿
        self.dt_mu = r.uniform(0.0105, 0.0165)      # 鼠标采样间隔
        self.dt_sigma = r.uniform(0.0022, 0.0040)
        # 同一个真实鼠标会稳定工作在某个报告率。125Hz(8ms) 最常见，部分设备
        # 使用 250Hz(4ms)；调度抖动只能围绕采样周期，不能把间隔铺成连续分布。
        self.sample_period = 0.008 if r.random() < 0.72 else 0.004
        # 习惯性落点偏移（占元素尺寸的比例，有人总爱点偏左上）
        self.landing_bias = (r.gauss(0.0, 0.055), r.gauss(0.0, 0.055))
        # 路径风格权重
        base = {'line': 0.22, 'arc': 0.28, 'line_then_arc': 0.40, 'overshoot_back': 0.10}
        w = {k: max(0.03, v * r.uniform(0.65, 1.45)) for k, v in base.items()}
        total = sum(w.values())
        self.style_weights = {k: v / total for k, v in w.items()}

    def __repr__(self):
        return ('MotionProfile(speed=%.2f curv=%.2f tremor=%.2f skew=%.2f '
                'hold=%.3f gap=%.3f)' % (self.speed, self.curvature, self.tremor,
                                         self.skew, self.hold_mu, self.gap_mu))


_DEFAULT_PROFILE = MotionProfile(random.Random(20260804))


def _prof(profile):
    return profile if profile is not None else _DEFAULT_PROFILE


# --------------------------------------------------------------------------- #
# 数学件
# --------------------------------------------------------------------------- #
def _bez2(p0, c, p1, t):
    mt = 1.0 - t
    return (mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0],
            mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1])


def _bez3(p0, c1, c2, p1, t):
    mt = 1.0 - t
    a, b, c, d = mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0],
            a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1])


def _velocity(t, skew):
    """
    非对称速度曲线：先弹道后校正。

    skew < 1 让前段更快。skew=0.75 时，50% 的时间已经走完约 67% 的距离，
    与真人指向运动的「快速接近 + 缓慢修正」一致。skew=1 退化为对称的
    smootherstep（最小急动度曲线）。
    """
    tt = t ** skew
    return tt * tt * tt * (tt * (tt * 6 - 15) + 10)


def _smooth_noise(n, rng, smooth=3):
    """低频噪声。逐点白噪声像静电，真实手抖是低频的。"""
    raw = [rng.uniform(-1.0, 1.0) for _ in range(n)]
    if smooth <= 1 or n < 3:
        return raw
    half = smooth // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(raw[lo:hi]) / float(hi - lo))
    return out


def _edge_weight(i, n, keep_head, keep_tail):
    """两端衰减权重：起点要接得上，终点要落得准。"""
    if n <= 1:
        return 0.0
    w = 1.0
    if i < keep_head:
        w = min(w, i / float(max(1, keep_head)))
    tail_i = n - 1 - i
    if tail_i < keep_tail:
        w = min(w, tail_i / float(max(1, keep_tail)))
    return max(0.0, min(1.0, w))


def _pick_style(rng, profile, dist):
    if dist <= SHORT_DIST:
        # 短距离几乎都是直的，偶尔带点弧
        return 'arc' if rng.random() < 0.30 else 'line'
    threshold, acc = rng.random(), 0.0
    for name in _PATH_STYLES:
        acc += profile.style_weights.get(name, 0.0)
        if threshold <= acc:
            return name
    return _PATH_STYLES[-1]


# --------------------------------------------------------------------------- #
# 主路径
# --------------------------------------------------------------------------- #
def path(x0, y0, x1, y1, rng=None, overshoot=True, min_step=1.0,
         profile=None, drag=False, target_w=None):
    """
    生成 (x0,y0) -> (x1,y1) 的拟人轨迹（物理像素，浮点）。

    返回 [(x, y), ...]，已保证相邻点取整后至少差 min_step 像素。
    drag=True 时不做过冲、不做末端回弹（滑块类控件冲过头会被判失败）。

    ``target_w`` 是目标可点区域的尺寸（像素），知道就按费茨定律把小目标的
    时长拉长；拿不到就完全按老口径走。
    """
    rng = rng or random
    p = _prof(profile)
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1.5:
        return [(x1, y1)]

    ux, uy = dx / dist, dy / dist
    nx, ny = -uy, ux                       # 法向
    style = 'line' if drag else _pick_style(rng, p, dist)
    short = dist <= SHORT_DIST

    # ---- 采样点数：按「时长 × 采样率」，不按距离 ----
    # 真人 125Hz 鼠标移动 600px 约 0.35s，即 ~30 个采样点，不是上百个。
    duration = min(0.11 + dist / 1900.0, 0.72)
    if target_w:
        # 费茨定律 MT = a + b·log2(2D/W)：同样距离，小目标明显更慢——最后
        # 那一段要减速瞄准。原来只有距离项，一个 20px 的复选框和一个 300px
        # 的按钮耗时完全一样，而真人差出一倍。
        #
        # 只允许把时长**拉长**：大目标继续走原来那条曲线。否则等于凭空多出
        # 一批「比以前更快」的移动，那是往反方向走。
        index = math.log2(max(2.0 * dist / max(float(target_w), 1.0), 2.0))
        duration = min(max(duration, 0.09 + 0.085 * index), 1.05)
    duration = duration / p.speed * rng.uniform(0.86, 1.18)
    n = int(min(max(duration / p.dt_mu, 7), 70))

    # ---- 曲率：按风格分别给幅度 ----
    # 用同一个上限压所有风格的话，arc 这种「本该明显绕一下」的路径会被压平，
    # 整体弯曲度分布挤成一坨，形状家族反而变单一。
    if short:
        lo, hi, cap = 0.010, 0.038, 11.0        # 短距离一律近直线（费茨定律）
    else:
        lo, hi, cap = _STYLE_AMP.get(style, (0.020, 0.070))[0], \
                      _STYLE_AMP.get(style, (0.020, 0.070))[1], 110.0
    amp = min(max(dist * rng.uniform(lo, hi), 2.0), cap) * p.curvature
    side = 1.0 if (rng.random() + p.arc_side * 0.5) > 0.5 else -1.0

    # ---- 终点：长距离按人格概率冲过头 ----
    end = (x1, y1)
    did_overshoot = False
    if overshoot and not drag and not short and rng.random() < p.overshoot_prob:
        over = rng.uniform(3.0, 9.0) * p.tremor
        end = (x1 + ux * over, y1 + uy * over)
        did_overshoot = True

    # ---- 按拓扑生成基础路径 ----
    def sample(fn):
        return [fn(_velocity(i / float(n), p.skew)) for i in range(1, n + 1)]

    if style == 'line':
        c1 = (x0 + dx * 0.33 + nx * amp * side * 0.25,
              y0 + dy * 0.33 + ny * amp * side * 0.25)
        c2 = (x0 + dx * 0.70 + nx * amp * side * 0.15,
              y0 + dy * 0.70 + ny * amp * side * 0.15)
        raw = sample(lambda t: _bez3((x0, y0), c1, c2, end, t))

    elif style == 'arc':
        c = (x0 + dx * 0.5 + nx * amp * side,
             y0 + dy * 0.5 + ny * amp * side)
        raw = sample(lambda t: _bez2((x0, y0), c, end, t))

    elif style == 'line_then_arc':
        ratio = rng.uniform(0.42, 0.70)
        mid = (x0 + dx * ratio, y0 + dy * ratio)
        n1 = max(2, int(n * ratio))
        n2 = max(2, n - n1)
        c0 = (x0 + (mid[0] - x0) * 0.5 + nx * amp * side * 0.12,
              y0 + (mid[1] - y0) * 0.5 + ny * amp * side * 0.12)
        seg1 = [_bez2((x0, y0), c0, mid, _velocity(i / float(n1), p.skew))
                for i in range(1, n1 + 1)]
        c1 = (mid[0] + (end[0] - mid[0]) * 0.5 + nx * amp * side,
              mid[1] + (end[1] - mid[1]) * 0.5 + ny * amp * side)
        seg2 = [_bez2(mid, c1, end, i / float(n2)) for i in range(1, n2 + 1)]
        raw = seg1 + seg2

    else:                                   # overshoot_back
        over = rng.uniform(10.0, 26.0) * p.tremor
        far = (x1 + ux * over + nx * amp * side * 0.4,
               y1 + uy * over + ny * amp * side * 0.4)
        n1 = max(3, int(n * 0.62))
        n2 = max(2, n - n1)
        seg1 = [_bez2((x0, y0),
                      (x0 + (far[0] - x0) * 0.55 + nx * amp * side * 0.5,
                       y0 + (far[1] - y0) * 0.55 + ny * amp * side * 0.5),
                      far, _velocity(i / float(n1), p.skew * 0.85))
                for i in range(1, n1 + 1)]
        cb = (far[0] + (x1 - far[0]) * 0.5 - nx * amp * side * 0.55,
              far[1] + (y1 - far[1]) * 0.5 - ny * amp * side * 0.55)
        seg2 = [_bez2(far, cb, (x1, y1), i / float(n2)) for i in range(1, n2 + 1)]
        raw = seg1 + seg2
        end = (x1, y1)
        did_overshoot = False

    # ---- 法向 / 切向噪声，两端衰减 ----
    m = len(raw)
    keep_head = max(2, int(m * 0.12))
    keep_tail = max(2, int(m * 0.16))
    norm_amp = (0.5 if short else 1.15) * p.tremor
    tan_amp = (0.25 if short else 0.55) * p.tremor
    nrm = _smooth_noise(m, rng, smooth=3)
    tan = _smooth_noise(m, rng, smooth=5)
    pts = []
    for i, (px, py) in enumerate(raw):
        w = _edge_weight(i, m, keep_head, keep_tail)
        jn = nrm[i] * norm_amp * w
        jt = tan[i] * tan_amp * w
        pts.append((px + nx * jn + ux * jt, py + ny * jn + uy * jt))

    # ---- 过冲后的回修 ----
    if did_overshoot:
        for k in range(1, 4):
            t = k / 3.0
            pts.append((end[0] + (x1 - end[0]) * t, end[1] + (y1 - end[1]) * t))

    # ---- 末端微整定：冲过一点点再收回来 ----
    # 两个前提：
    #   1. 幅度取整后必须 >=1px，否则被 _dedup 吃掉等于没做
    #      （tremor 最低 0.6，直接乘会把 1.2px 压到 0.72px）
    #   2. 路径级已经过冲过一次的就不再来一次 —— 真人一次移动只有一次
    #      「冲过去再修正」，叠两次反而不像
    if (not drag and not did_overshoot and dist > 20
            and rng.random() < p.settle_prob):
        tiny = max(1.4, rng.uniform(1.2, 3.2) * p.tremor)
        back = rng.uniform(0.55, 0.9)
        pts.append((x1 + ux * tiny, y1 + uy * tiny))
        pts.append((x1 - ux * tiny * back, y1 - uy * tiny * back))
    pts.append((x1, y1))

    return _dedup(pts, min_step)


def _dedup(pts, min_step=1.0):
    """
    丢掉取整后与上一点重合的点。

    缓动曲线的头尾天然是亚像素步长，不去重的话那些点会被 Chromium 静默丢弃，
    白白占用一次 CDP/消息往返，而且实际缓动效果与设计不符。
    """
    out, last = [], None
    for pt in pts:
        ip = (int(round(pt[0])), int(round(pt[1])))
        if last is not None and abs(ip[0] - last[0]) < min_step and \
                abs(ip[1] - last[1]) < min_step:
            continue
        out.append(pt)
        last = ip
    if not out and pts:
        out = [pts[-1]]
    return out


def drag_path(x0, y0, x1, y1, rng=None, profile=None):
    """拖拽轨迹：不过冲、不回弹（滑块类控件冲过头会被判失败），点更密。"""
    return path(x0, y0, x1, y1, rng=rng, overshoot=False,
                profile=profile, drag=True)


# --------------------------------------------------------------------------- #
# 时序
# --------------------------------------------------------------------------- #
def move_interval(rng=None, profile=None):
    """鼠标采样间隔（秒）：按会话报告率量化，并叠加少量调度抖动。"""
    rng = rng or random
    p = _prof(profile)
    period = float(getattr(p, "sample_period", 0.008))
    target = p.dt_mu / p.speed
    multiple = max(1, int(round(target / period)))
    roll = rng.random()
    if roll < 0.12 and multiple > 1:
        multiple -= 1
    elif roll > 0.88:
        multiple += 1
    v = multiple * period + rng.gauss(0.0, period * 0.035)
    return min(max(v, max(0.0035, period * 0.72)), 0.045)


def _bounded(rng, value, low, high):
    """把值收进 [low, high]，但**不**把越界的采样压成同一个数。

    `min(max(v, low), high)` 看着无害，实际会让一批采样落在**完全相同**的
    边界值上。按单个会话（一个会话只有一个人格）实测：最坏的会话里
    26% 的滚轮间隔正好是 28.000ms、18% 的点击前停顿正好是 30.000ms、
    15% 的按住时长正好是 35.000ms。一整段操作里几百个一模一样的数，
    比数值本身偏快好查得多——这正是「固定值」那一类特征。

    跨人格取平均会把它平均掉（平均只有几个百分点），必须按单人格看最坏情况。

    物理边界本来也不是一个精确值，所以越界时在边界内侧随机取一小段：
    既不越过边界，也不制造尖峰。
    """
    if value < low:
        return low * rng.uniform(1.0, 1.12)
    if value > high:
        return high * rng.uniform(0.90, 1.0)
    return value


def click_hold(rng=None, profile=None):
    rng = rng or random
    p = _prof(profile)
    return _bounded(rng, rng.gauss(p.hold_mu, 0.018), 0.035, 0.20)


def pre_click_pause(rng=None, profile=None):
    rng = rng or random
    p = _prof(profile)
    return _bounded(rng, rng.gauss(0.09 / p.speed, 0.035), 0.03, 0.32)


def key_hold(rng=None, profile=None):
    rng = rng or random
    p = _prof(profile)
    return _bounded(rng, rng.gauss(p.hold_mu * 0.85, 0.015), 0.030, 0.14)


#: 不同字符打起来快慢差很多，一刀切用同一个分布不像真人。
#: 实测这个程序真正敲进表单的字符构成：数字 44%、大写 30%、小写 19%、符号 7%
#: ——「美国人打英文字母快」只作用于不到两成的按键。
#: 小写在主键区、肌肉记忆最强；数字在数字行，多数人慢 1.5~2 倍；
#: 大写和符号要多按一次 Shift。
_LETTER, _SHIFTED, _DIGIT = 1.0, 1.5, 1.8


def char_gap_factor(ch) -> float:
    """按字符内容自动判断这一下该慢多少（相对小写字母的倍数）。"""
    if not isinstance(ch, str) or not ch:
        return _LETTER
    if ch.isdigit():
        return _DIGIT
    if ch.isupper():
        return _SHIFTED
    if ch.isalpha() or ch in " 	":
        return _LETTER
    return _SHIFTED          # 标点、@、括号之类：要么按 Shift，要么够得远


#: Shift 打出来的符号回到它所在的物理键，指法按那个键算。
_SHIFT_BASE = {
    '~': '`', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
    '^': '6', '&': '7', '*': '8', '(': '9', ')': '0', '_': '-',
    '+': '=', '{': '[', '}': ']', '|': '\\', ':': ';', '"': "'",
    '<': ',', '>': '.', '?': '/',
}

#: US QWERTY 标准指法：每行按 (行号, 左手, 右手)，手内按 食指/中指/无名指/小指 排。
#:
#: 这张表是从一个真实工具的按键延迟 switch 里还原出来的——它按虚拟键码分成
#: 50/100/150/200/250ms 五档，画到键盘上正好是食指到小指五列：
#: 50 档 `B G H N T Y` 是食指内伸、100 档 `F J M R U V` 是食指本位列、
#: 150 档 `C D E I K ,` 是中指列、200 档 `L O S W X .` 是无名指列、
#: 250 档 `A P Q Z /` 是小指列。三个符号 `, . /` 各自和同列字母取值完全一致，
#: 排除了巧合——分档与标准指法逐键吻合，确认是手指模型。
#:
#: 只取它的**手指归属**，不取那套常数：它的空格是 10ms 固定且不参与随机
#: （整段文本里每个空格间隔一模一样，这本身就是最好查的指纹），50ms 档也低于
#: 同指换键的物理极限。间隔仍然由 key_gap 的对数正态分布决定。
_LAYOUT_ROWS = (
    (1, ("45", "3", "2", "1`"), ("67", "8", "9", "0-=")),
    (2, ("rt", "e", "w", "q"), ("yu", "i", "o", "p[]\\")),
    (3, ("fg", "d", "s", "a"), ("hj", "k", "l", ";'")),
    (4, ("vb", "c", "x", "z"), ("nm", ",", ".", "/")),
)


def _build_key_sites():
    sites = {}
    for row, left, right in _LAYOUT_ROWS:
        for hand, group in (("L", left), ("R", right)):
            for finger, keys in enumerate(group, start=1):
                for key in keys:
                    sites[key] = (hand, finger, row)
    sites[" "] = ("T", 0, 5)              # 拇指，任何一只手都能敲
    return sites


_KEY_SITES = _build_key_sites()


def key_site(ch):
    """这个字符落在哪只手、哪根手指、第几行；不在 US 布局上返回 None。"""
    if not isinstance(ch, str) or len(ch) != 1:
        return None
    return _KEY_SITES.get(_SHIFT_BASE.get(ch, ch).lower())


#: 相对中指的手指代价：越靠外移动越慢。顺序取自上面那张表的档位
#: （拇指 < 食指 < 中指 < 无名指 < 小指），但不用它 5 倍的极差——
#: 那是它函数内部的一个中间量，不是整个键间隔。
_FINGER_COST = {0: 0.88, 1: 0.94, 2: 1.00, 3: 1.08, 4: 1.16}


def digram_factor(prev, ch) -> float:
    """「上一个键 → 这个键」的相对快慢。

    真人打字的间隔主要由这一对键决定，而不是单个键：**交替手最快**（一只手在敲，
    另一只已经移到位了），同手不同指居中，**同一根手指换键最慢**——手指得先抬起来
    横着移过去，实测慢 1.5~2 倍。再叠上跨了几行。

    同一个键连打（`ll`、`00`）是特例：手指不用移动，回弹就能再敲一次，反而快。
    """
    a, b = key_site(prev), key_site(ch)
    if a is None or b is None:
        return 1.0
    cost = _FINGER_COST.get(b[1], 1.0)
    if a[0] != b[0]:
        return cost * 0.86                       # 交替手
    rows = abs(a[2] - b[2])
    if a[1] != b[1]:
        return cost * (1.18 + 0.09 * rows)       # 同手不同指
    if str(prev).lower() == str(ch).lower():
        return cost * 1.02                       # 同一个键连打
    return cost * (1.85 + 0.10 * rows)           # 同指换键：最慢


def digram_floor(prev, ch) -> float:
    """这一对键物理上最快能有多快。

    原来是一刀切 60ms，只因为当时拿不到「敲的是哪个键」。有了指法表就能分开：
    交替手实测能到 50ms，而同一根手指换键连 100ms 都很勉强。
    """
    a, b = key_site(prev), key_site(ch)
    if a is None or b is None:
        return 0.060
    if a[0] != b[0]:
        return 0.050
    if a[1] == b[1] and str(prev).lower() != str(ch).lower():
        return 0.100
    return 0.060


def key_gap(rng=None, profile=None, ch=None, prev=None):
    """键间隔：对数正态 + 偶发思考停顿 + 字符类别 + 「上一个键 → 这个键」。

    `prev` 传了才按键对算；不传就退回按单字符的老口径——退格、DrissionPage
    兼容入口这些拿不到上一个键的调用点照常能用。
    """
    rng = rng or random
    p = _prof(profile)
    centre = p.gap_mu * char_gap_factor(ch) / p.speed
    floor = 0.060
    if prev is not None:
        centre *= digram_factor(prev, ch)
        floor = digram_floor(prev, ch)
    v = rng.lognormvariate(math.log(centre), p.gap_sigma)
    if rng.random() < p.pause_prob:
        v += rng.uniform(0.25, 0.95)
    # 两头都不能硬夹。`max(v, floor)` 会把一批按键压到**同一个数**上：实测
    # 偏快的人格里最多有三分之一的空格间隔正好是 50.000ms，一整段文本里几百个
    # 一模一样的值，比间隔偏快好查得多——那正是「空格固定 10ms」的翻版，
    # 只是从下限这头撞上去。物理下限本来也不是一个精确值，所以在边界之上
    # 随机取一段，既不越过物理边界，也不制造尖峰。上限同理。
    if v < floor:
        return floor * rng.uniform(1.0, 1.22)
    if v > 1.8:
        return rng.uniform(1.62, 1.8)
    return v


def wheel_gap(rng=None, profile=None):
    rng = rng or random
    p = _prof(profile)
    return _bounded(rng, rng.gauss(0.055 / p.speed, 0.016), 0.028, 0.13)


def jitter_point(x, y, w, h, rng=None, bias_up=True, profile=None):
    """
    元素内的拟人落点：二维高斯 + 该人格的习惯性偏移。

    DP 原实现恒为 (中点X, padding顶边+3)，同一元素点一百次落点像素级完全相同 ——
    轨迹越拟人，固定落点反而越突兀。
    """
    rng = rng or random
    p = _prof(profile)
    cx = x + w * (0.5 + p.landing_bias[0])
    cy = y + h * ((0.42 if bias_up else 0.5) + p.landing_bias[1])
    sx = max(w / 6.5, 1.0)
    sy = max(h / 6.5, 1.0)
    for _ in range(12):
        px, py = rng.gauss(cx, sx), rng.gauss(cy, sy)
        if x + 1 <= px <= x + w - 1 and y + 1 <= py <= y + h - 1:
            return px, py
    return x + w / 2.0, y + h / 2.0
