#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rf4_updater_gui.py — RF4 钓鱼登录器自动更新器（图形界面版）

行为：
  打开后自动判断是否需要更新：
    * 需要更新 -> 自动从网盘下载、校验、替换，完成后自动启动登录器
    * 已是最新 -> 直接自动启动登录器
  3 秒倒计时后自动启动登录器；启动成功后更新器自己关窗退出，
  出错时保留窗口以便查看日志。

界面按 Chinese Fishing 4 的湖边垂钓主题手绘（夜色湖面、芦苇、浮漂、鱼影）。
只依赖 Python 标准库（tkinter），可整盘复制 rf4-auto-update 文件夹到别处使用。
"""
import os
import sys
import threading
import queue
import time
import hashlib
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DRY_RUN = bool(os.environ.get("RF4_DRY_RUN"))  # 演练模式：不真的启动登录器
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.font as tkfont

import rf4_updater as U

TITLE = "RF4 钓鱼登录器 · 自动更新"

# ------------------------------- 配色 -------------------------------
C_BG      = "#07222f"   # 深夜底色
C_PANEL   = "#0d3345"   # 面板
C_PANEL2  = "#124056"   # 面板亮
C_LINE    = "#1d5a75"   # 描边
C_TEXT    = "#e8f6fa"   # 主文字
C_MUTED   = "#8fbcca"   # 次要文字
C_GOLD    = "#f2b134"   # 太阳鱼金
C_ORANGE  = "#e8743b"   # 落日
C_GREEN   = "#4fb781"   # 芦苇
C_RED     = "#e2483d"   # 浮漂红
C_WATER1  = "#0a4f6b"   # 水面
C_WATER2  = "#052532"   # 水深

STAGES = [
    ("准备",                    0.00, 0.06),
    ("检查本地版本",            0.06, 0.12),
    ("连接网盘读取列表",        0.12, 0.22),
    ("解析下载直链",            0.22, 0.32),
    ("通过网盘安全验证",        0.32, 0.42),
    ("下载登录器",              0.42, 0.82),
    ("校验文件",                0.82, 0.88),
    ("比对版本",                0.88, 0.92),
    ("备份并替换旧版",          0.92, 0.99),
    ("完成",                    1.00, 1.00),
]


class Progress:
    """把某个阶段映射到全局进度带。"""

    def __init__(self, q):
        self.q = q
        self.i = 0
        self.lo = 0.0
        self.hi = 0.0

    def stage(self, name, text=None):
        for i, (n, lo, hi) in enumerate(STAGES):
            if n == name:
                self.i = i
                self.lo, self.hi = lo, hi
                break
        if text:
            self.q.put(("log", text))

    def frac(self, f, text=None):
        f = max(0.0, min(1.0, f))
        p = round((self.lo + (self.hi - self.lo) * f) * 100)
        self.q.put(("set", (p, STAGES[self.i][0])))
        if text:
            self.q.put(("log", text))


# --------------------------- 网络（带进度） ---------------------------
class GuiLanzou(U.Lanzou):
    def resolve_final_url(self, dl, wait_cb=None):
        """下载直链 -> interstitial 二次验证 -> 真实文件 URL；等待动画期间回调进度。"""
        import urllib.parse
        referer = "https://" + U.HOSTS[0] + "/"
        resp, body = self.s.fetch(dl + "&toolsdown", referer=referer, maxlen=2_000_000)
        if body[:2] == b"MZ":
            return self.s.fetch(resp.geturl(), referer=referer, maxlen=1 << 30)
        txt = body.decode("utf-8", "replace")
        if "down_r" not in txt:
            raise RuntimeError("下载返回异常内容: " + txt[:160])
        mfile = re_search(r"'file':'([A-Za-z0-9+/=]+)'", txt)
        msign = re_search(r"'sign':'([A-Za-z0-9+/=]+)'", txt)
        ajax_url = urllib.parse.urljoin(resp.geturl(), "ajax.php")
        origin = urllib.parse.urlsplit(resp.geturl())
        if wait_cb:
            for i in range(U.GATE_DELAY * 5):
                wait_cb(i / (U.GATE_DELAY * 5))
                time.sleep(0.2)
        _, body2 = self.s.fetch(
            ajax_url, referer=resp.geturl(),
            data={"file": mfile, "el": "2", "sign": msign},
            headers={"X-Requested-With": "XMLHttpRequest",
                    "Origin": origin.scheme + "://" + origin.netloc},
            maxlen=6000)
        import json
        j = json.loads(body2.decode("utf-8", "replace"))
        if str(j.get("zt")) != "1" or not j.get("url") or "SignError" in str(j.get("url")):
            raise RuntimeError("网盘二次验证未通过: %s" % json.dumps(j, ensure_ascii=False)[:160])
        final = j["url"] if str(j["url"]).startswith("http") \
            else urllib.parse.urljoin(ajax_url, j["url"])
        return self.s.fetch(final, referer=resp.geturl(), maxlen=1 << 30)

    def stream_to(self, url, referer, dest, on_progress, should_stop):
        """流式下载到 dest，实时回调字节进度。"""
        tmp = dest + ".part"
        h = hashlib.sha256()
        done = 0
        total = 0
        head = None
        with open(tmp, "wb") as fp:
            for attempt in range(4):
                req = self.s._rq(url, referer)
                resp = self.s.op.open(req, timeout=180)
                head = resp.read(2)
                resp.close()
                if head == b"MZ":
                    break
                # WAF 挑战页：解一次 cookie 再重试
                resp2, body = self.s.fetch(url, referer=referer, maxlen=200_000)
                url = resp2.geturl()
                if body[:2] == b"MZ":
                    head = b"MZ"
                    break
                if attempt == 3:
                    raise RuntimeError("下载被拦在验证页，未取得文件")
            req = self.s._rq(url, referer)
            resp = self.s.op.open(req, timeout=300)
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            first = b""
            with open(tmp, "wb") as fp:
                while True:
                    if should_stop():
                        raise KeyboardInterrupt("已取消")
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    if not first:
                        first = chunk[:2]
                        if first != b"MZ":
                            raise RuntimeError("下载内容不是可执行文件")
                    fp.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    on_progress(done, total)
            resp.close()
        os.replace(tmp, dest)
        return {"size": done, "sha256": h.hexdigest(), "total": total}


def re_search(pattern, text):
    import re
    m = re.search(pattern, text)
    if not m:
        raise RuntimeError("验证页缺少 token")
    return m.group(1)


# ------------------------------- 任务 -------------------------------
def job(q, stop_flag):
    """后台线程：判定是否需要更新 -> 需要则更新 -> 结果交给界面。"""
    def stopped():
        return stop_flag.is_set()

    p = Progress(q)
    try:
        p.stage("准备")
        p.frac(0.2, "游戏目录：%s（%s）" % (U.LAUNCHER_DIR, U.GAME_DIR_WHY))
        if not os.path.exists(U.LAUNCHER_EXE):
            p.frac(0.9, "登录器不存在，需要下载：%s" % U.TARGET_NAME)
        else:
            sz = os.path.getsize(U.LAUNCHER_EXE)
            p.frac(0.6, "当前登录器：%s （%.1f MB）" % (U.TARGET_NAME, sz / 1048576))
        p.frac(1.0, "检查完成")
        if stopped():
            return

        # ---- 本地版本与补丁要求 ----
        p.stage("检查本地版本")
        local_v = U.exe_required_version(U.LAUNCHER_EXE) if os.path.exists(U.LAUNCHER_EXE) else None
        req_v = None
        try:
            import json
            j = json.load(open(U.STATE_FILE, encoding="utf-8"))
            import re
            m = re.search(r"-v(\d+)-", j.get("version") or "")
            req_v = int(m.group(1)) if m else None
        except Exception:
            pass
        p.frac(0.5, "登录器内嵌要求版本：%s / 当前补丁要求：%s"
               % (("v%s" % local_v) if local_v else "未知",
                  ("v%s" % req_v) if req_v else "未知"))
        need_by_patch = (local_v is None) or (req_v is not None and local_v < req_v)
        p.frac(1.0, "需要更新" if need_by_patch else "本地版本满足补丁要求")
        if stopped():
            return

        # ---- 网盘列表 ----
        p.stage("连接网盘读取列表")
        lz = GuiLanzou()
        host, files = lz.list_files()
        p.frac(0.7, "已连接 %s" % host)
        f = next((x for x in files if x["name_all"] == U.TARGET_NAME), files[0])
        p.frac(1.0, "网盘文件：%s  %s  （更新于 %s）" % (f["name_all"], f["size"], f["time"]))
        st = U.load_state()
        local_sha = U.sha256_file(U.LAUNCHER_EXE) if os.path.exists(U.LAUNCHER_EXE) else None
        same_recorded = (st.get("last_file_id") == f["id"]
                         and (st.get("installed") or {}).get("sha256") == local_sha)
        if same_recorded and not need_by_patch:
            st["last_file_id"] = f["id"]
            U.save_state(st)        # 顺手记住游戏目录，搬家/重启后仍能定位
            q.put(("done", {"kind": "latest", "v": local_v, "id": f["id"], "host": host}))
            return
        if stopped():
            return

        # ---- 下载直链 ----
        p.stage("解析下载直链")
        dl = lz.download_url(host, f["id"])
        p.frac(1.0, "取得下载直链")
        if stopped():
            return

        # ---- 过验证 ----
        p.stage("通过网盘安全验证")
        resp, _ = lz.resolve_final_url(dl, wait_cb=lambda fr: p.frac(fr, None))
        final_url = resp.geturl()
        p.frac(1.0, "验证通过，开始下载")
        if stopped():
            return

        # ---- 流式下载 ----
        p.stage("下载登录器")
        dest = os.path.join(U.DOWNLOAD_DIR, U.TARGET_NAME)
        os.makedirs(U.DOWNLOAD_DIR, exist_ok=True)
        last = [0.0]

        def on_prog(done, total):
            now = time.time()
            if total:
                fr = done / total
            else:
                fr = min(0.99, done / 55_000_000)
            if now - last[0] > 0.25 or fr >= 1.0:
                last[0] = now
                p.frac(fr, None)

        meta = lz.stream_to(final_url, resp.geturl() if False else dest and final_url,
                            dest, on_prog, stopped)
        p.frac(1.0, "下载完成 %.1f MB" % (meta["size"] / 1048576))

        # ---- 校验 ----
        p.stage("校验文件")
        p.frac(0.5, "sha256 = " + meta["sha256"])
        p.frac(1.0, "校验通过")

        # ---- 比对版本 ----
        p.stage("比对版本")
        v_new = U.exe_required_version(dest)
        if local_sha == meta["sha256"]:
            q.put(("done", {"kind": "same", "v": local_v, "id": f["id"], "host": host}))
            st["last_file_id"] = f["id"]
            st["installed"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "to_v": v_new, "sha256": meta["sha256"],
                               "size": meta["size"], "result": "same"}
            U.save_state(st)
            return
        if local_v is not None and v_new is not None and v_new < local_v:
            raise RuntimeError("网盘版本 v%s 低于本地 v%s，拒绝降级下载" % (v_new, local_v))
        p.frac(1.0, "版本变化：v%s -> v%s" % (local_v, v_new))

        # ---- 替换 ----
        p.stage("备份并替换旧版")
        p.frac(0.4, "备份旧登录器并写入新版…")
        res = U.install(dest)
        p.frac(1.0, "替换完成" if res == "updated" else "无需替换")
        st = U.load_state()
        st["last_file_id"] = f["id"]
        st["installed"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "from_v": local_v, "to_v": v_new,
                           "sha256": meta["sha256"], "size": meta["size"],
                           "result": res}
        U.save_state(st)
        q.put(("done", {"kind": "updated", "from_v": local_v, "v": v_new,
                        "host": host, "id": f["id"]}))
    except KeyboardInterrupt:
        q.put(("info", "已取消。"))
        q.put(("quit", None))
    except Exception as e:
        try:
            q.put(("log", traceback.format_exc(limit=2)))
        except Exception:
            pass
        q.put(("error", "%s: %s" % (type(e).__name__, e)))


# ------------------------------ 界面绘制 ------------------------------
def mix(a, b, t):
    ca = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
    cb = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
    c = tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % c


class Scene(tk.Canvas):
    """夜色湖面主题头图：落日余晖、芦苇、钓线浮漂、游鱼剪影。"""

    def __init__(self, master, w=560, h=140):
        super().__init__(master, width=w, height=h, highlightthickness=0, bd=0,
                         bg=C_BG)
        self.w, self.h = w, h
        self.t = 0.0
        self._sky = []
        for i in range(28):
            f = i / 27
            self.create_rectangle(0, f * 62, w, f * 62 + 6, fill=mix(C_WATER1, C_ORANGE, 1 - f), width=0, tags="sky")
        self.lower = 62
        for i in range(24):
            f = i / 23
            self.create_rectangle(0, self.lower + f * (h - self.lower), w,
                                  self.lower + f * (h - self.lower) + 5,
                                  fill=mix("#0d5f7f", C_WATER2, f), width=0, tags="water")
        # 落日
        self.create_oval(w * 0.78 - 17, 22, w * 0.78 + 17, 56, fill="#ffd27a", outline="", tags="sun")
        self.create_rectangle(0, 58, w, 64, fill="#0a4a63", outline="", tags="horizon")
        # 水面波纹（随时间平移）
        self.ripples = []
        for k in range(5):
            y = 74 + k * 12
            pts = []
            for x in range(0, w + 40, 20):
                pts += [x, y + (3 if (x // 20) % 2 else -3)]
            self.ripples.append(self.create_line(*pts, smooth=True, fill="#1b6d8f", width=1))
        # 远山/树线
        self.create_polygon(0, 60, 60, 40, 120, 58, 190, 44, 250, 60, 0, 64,
                            fill="#062733", outline="", tags="ridge")
        # 芦苇（左侧）
        for i, (bx, bh) in enumerate([(24, 52), (40, 68), (56, 44), (72, 60), (14, 38)]):
            x, y1, y2 = bx, 62, 62 - bh
            self.create_line(x, y1, x + 6, y2, fill=C_GREEN, width=3, tags="reed%d" % i)
            self.create_oval(x + 3, y2 - 6, x + 10, y2 + 4, fill="#2f7f5b", outline="", tags="reed%d" % i)
        # 钓竿 + 钓线 + 浮漂
        self.create_line(w - 150, 16, w - 60, 44, fill="#c9a227", width=3, tags="rod")
        self.line_pts = self.create_line(w - 60, 44, w - 52, 92, fill="#9fd4e3", width=1, tags="line")
        self.bob = self.create_oval(w - 58, 88, w - 46, 100, fill=C_RED, outline="#ffffff", tags="bob")
        self.create_line(w - 52, 82, w - 52, 90, fill="#ffffff", width=1, tags="bob2")
        # 鱼影
        self.fish = []
        for i, (fy, sc, col) in enumerate([(96, 1.0, "#155a76"), (116, 0.75, "#123f52"), (132, 0.6, "#0e3446")]):
            self.fish.append((self._draw_fish(0, fy, sc, col), fy, sc, i))
        self.bind("<Destroy>", lambda e: self.after_cancel(self._job) if self._job else None)
        self._job = self.after(60, self._tick)

    def _draw_fish(self, x, y, sc, col):
        L, H = 46 * sc, 13 * sc
        body = self.create_polygon(
            x, y, x + L * 0.72, y - H, x + L, y, x + L * 0.72, y + H,
            fill=col, outline="", smooth=True, tags="fish")
        tail = self.create_polygon(
            x + L, y, x + L + 12 * sc, y - 9 * sc, x + L + 12 * sc, y + 9 * sc,
            fill=col, outline="", tags="fish")
        eye = self.create_oval(x + L * 0.18, y - 2 * sc, x + L * 0.18 + 3 * sc,
                               y - 2 * sc + 3 * sc, fill="#0a2b38", outline="", tags="fish")
        return (body, tail, eye)

    def _tick(self):
        try:
            self.t += 0.05
            # 鱼群缓慢巡游（从左到右，游出后回到左侧）
            for items, fy, sc, i in self.fish:
                span = self.w + 120
                x = (self.t * (18 + i * 9) + i * 170) % span - 60
                dy = 2.2 * __import__("math").sin(self.t * 1.6 + i)
                dx = x - (items[0][2] if False else 0)
                self._move_fish(items, x, fy + dy, sc)
            # 浮漂上下浮动 + 涟漪
            bob_dy = 2.5 * __import__("math").sin(self.t * 2.2)
            self.move("bob", 0, bob_dy - (self.bob_dy if hasattr(self, "bob_dy") else 0))
            self.bob_dy = bob_dy
            self.move("line", 0, (bob_dy - self.line_dy) * 0.5 if hasattr(self, "line_dy") else 0)
            self.line_dy = bob_dy
            self.itemconfig(self.ripples[0], start=(self.t * 6) % 20)
            self._job = self.after(60, self._tick)
        except tk.TclError:
            self._job = None

    def _move_fish(self, items, x, y, sc):
        body, tail, eye = items
        L, H = 46 * sc, 13 * sc
        self.coords(body, x, y, x + L * 0.72, y - H, x + L, y, x + L * 0.72, y + H)
        self.coords(tail, x + L, y, x + L + 12 * sc, y - 9 * sc, x + L + 12 * sc, y + 9 * sc)
        self.coords(eye, x + L * 0.18, y - 2 * sc, x + L * 0.18 + 3 * sc, y - 2 * sc + 3 * sc)


def build_style():
    s = ttk.Style()
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass
    s.configure(".", background=C_BG, foreground=C_TEXT, fieldbackground=C_PANEL,
                bordercolor=C_LINE, lightcolor=C_PANEL, darkcolor=C_PANEL)
    s.configure("TFrame", background=C_BG)
    s.configure("Card.TFrame", background=C_PANEL, relief="flat")
    s.configure("TLabel", background=C_BG, foreground=C_TEXT)
    s.configure("Card.TLabel", background=C_PANEL, foreground=C_TEXT)
    s.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED)
    s.configure("CardMuted.TLabel", background=C_PANEL, foreground=C_MUTED)
    s.configure("Title.TLabel", background=C_BG, foreground=C_TEXT, font=("Microsoft YaHei UI", 13, "bold"))
    s.configure("Sub.TLabel", background=C_BG, foreground=C_GOLD, font=("Microsoft YaHei UI", 9))
    s.configure("Big.TLabel", background=C_PANEL, foreground=C_TEXT, font=("Microsoft YaHei UI", 10, "bold"))
    s.configure("TButton", background=C_PANEL2, foreground=C_TEXT, bordercolor=C_LINE,
                focuscolor=C_PANEL2, padding=(12, 6), font=("Microsoft YaHei UI", 9), relief="flat")
    s.map("TButton", background=[("active", "#1a5c7a"), ("disabled", C_PANEL)],
          foreground=[("disabled", "#5f8595")])
    s.configure("Go.TButton", background=C_GOLD, foreground="#12303f", font=("Microsoft YaHei UI", 9, "bold"))
    s.map("Go.TButton", background=[("active", "#ffc94d")])
    s.configure("Green.TButton", background=C_GREEN, foreground="#082a1c", font=("Microsoft YaHei UI", 9, "bold"))
    s.map("Green.TButton", background=[("active", "#63cd95")])
    s.configure("TProgressbar", background=C_GOLD, troughcolor=C_PANEL,
                bordercolor=C_LINE, lightcolor=C_GOLD, darkcolor=C_GOLD, thickness=16)
    s.configure("Treeview", background=C_PANEL, fieldbackground=C_PANEL, foreground=C_TEXT,
                bordercolor=C_LINE, rowbackground=C_PANEL)
    return s


# ------------------------------- 主窗口 -------------------------------
def gui():
    root = tk.Tk()
    root.title(TITLE)
    root.configure(bg=C_BG)
    build_style()

    W, H = 600, 560
    root.geometry("%dx%d+120+80" % (W, H))
    root.minsize(560, 520)

    q = queue.Queue()
    stop_flag = threading.Event()
    worker = [None]
    state = {"done": None}

    # 头部主题画布
    scene = Scene(root, W, 148)
    scene.pack(fill="x")
    head = tk.Frame(root, bg=C_BG)
    head.pack(fill="x", padx=18, pady=(6, 2))
    tk.Label(head, text=TITLE, bg=C_BG, fg=C_TEXT,
             font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
    tk.Label(head, text="Chinese Fishing 4 · 蓝奏云自动取最新版 · 校 sha256 · 自动备份替换",
             bg=C_BG, fg=C_GOLD, font=("Microsoft YaHei UI", 9)).pack(anchor="w")

    # 信息卡
    card = tk.Frame(root, bg=C_PANEL, highlightbackground=C_LINE, highlightthickness=1)
    card.pack(fill="x", padx=18, pady=10)
    inner = tk.Frame(card, bg=C_PANEL)
    inner.pack(fill="x", padx=14, pady=12)
    info = {}
    rows = [("游戏目录", "dir"), ("本地登录器", "local"), ("网盘版本", "remote"), ("状态", "status")]
    for i, (label, key) in enumerate(rows):
        tk.Label(inner, text=label, bg=C_PANEL, fg=C_MUTED,
                 font=("Microsoft YaHei UI", 9)).grid(row=i, column=0, sticky="w", padx=(0, 12), pady=2)
        v = tk.Label(inner, text="…", bg=C_PANEL, fg=C_TEXT,
                     font=("Microsoft YaHei UI", 9, "bold"), anchor="w", justify="left", wraplength=380)
        v.grid(row=i, column=1, sticky="w", pady=2)
        info[key] = v
    info["dir"].config(text="%s\n（%s）" % (U.LAUNCHER_DIR, U.GAME_DIR_WHY), justify="left")

    # 进度
    prow = tk.Frame(root, bg=C_BG)
    prow.pack(fill="x", padx=18)
    bar = ttk.Progressbar(prow, mode="determinate", maximum=100)
    bar.pack(fill="x")
    srow = tk.Frame(root, bg=C_BG)
    srow.pack(fill="x", padx=18, pady=(4, 0))
    pct = tk.Label(srow, text="0%", bg=C_BG, fg=C_GOLD, width=5,
                   font=("Consolas", 11, "bold"))
    pct.pack(side="left")
    stage = tk.Label(srow, text="准备就绪", bg=C_BG, fg=C_MUTED, anchor="w",
                     font=("Microsoft YaHei UI", 9))
    stage.pack(side="left", fill="x", expand=True)

    # 日志
    logf = tk.Frame(root, bg=C_BG)
    logf.pack(fill="both", expand=True, padx=18, pady=(8, 6))
    txt = tk.Text(logf, height=8, bg="#061c26", fg="#bfe3ee", insertbackground="#bfe3ee",
                  relief="flat", highlightbackground=C_LINE, highlightthickness=1,
                  font=("Consolas", 9), wrap="word", padx=8, pady=6, state="disabled")
    txt.pack(fill="both", expand=True)

    # 按钮
    btns = tk.Frame(root, bg=C_BG)
    btns.pack(fill="x", padx=18, pady=(0, 14))
    countdown = tk.Label(btns, text="", bg=C_BG, fg=C_GREEN,
                         font=("Microsoft YaHei UI", 9, "bold"))
    countdown.pack(side="left")
    b_cancel = ttk.Button(btns, text="取消")
    b_retry = ttk.Button(btns, text="重试")
    b_launch = ttk.Button(btns, text="启动登录器", style="Green.TButton", state="disabled")
    b_close = ttk.Button(btns, text="关闭")
    for b in (b_close, b_launch, b_retry, b_cancel):
        b.pack(side="right", padx=(6, 0))

    def append(line):
        txt.config(state="normal")
        txt.insert("end", line.rstrip() + "\n")
        txt.see("end")
        txt.config(state="disabled")

    def start():
        if worker[0] and worker[0].is_alive():
            return
        stop_flag.clear()
        state["done"] = None
        b_launch.config(state="disabled")
        b_retry.config(state="normal")
        b_cancel.config(state="normal")
        countdown.config(text="")
        bar.config(value=0)
        append("—— 开始检查 ——")
        worker[0] = threading.Thread(target=job, args=(q, stop_flag), daemon=True)
        worker[0].start()

    def launch():
        """启动游戏登录器本体。"""
        if DRY_RUN:
            append("（演练模式）跳过启动登录器：%s" % U.LAUNCHER_EXE)
            return True
        try:
            os.startfile(U.LAUNCHER_EXE)
            append("已启动登录器：%s" % U.LAUNCHER_EXE)
            return True
        except Exception as e:
            append("启动登录器失败：%r" % e)
            info["status"].config(text="启动失败：" + repr(e)[:80], fg=C_RED)
            return False

    AUTO_SECONDS = 3
    auto_left = [0]
    auto_job = [None]

    def cancel_countdown():
        if auto_job[0]:
            try:
                root.after_cancel(auto_job[0])
            except Exception:
                pass
        auto_job[0] = None
        auto_left[0] = 0

    def launch_and_close():
        """启动登录器；成功后本窗口自动退出，失败则保留窗口。"""
        cancel_countdown()
        countdown.config(text="")
        if launch():
            info["status"].config(text="已启动登录器，更新器自动退出…", fg=C_GREEN)
            for b in (b_launch, b_cancel, b_retry, b_close):
                b.config(state="disabled")
            root.after(900, root.destroy)

    def tick_countdown():
        auto_job[0] = None
        if state["done"] is None or auto_left[0] <= 0:
            return
        auto_left[0] -= 1
        if auto_left[0] > 0:
            countdown.config(text="%d 秒后启动登录器并退出（点\"取消\"可留下）" % auto_left[0])
            auto_job[0] = root.after(1000, tick_countdown)
        else:
            launch_and_close()

    def finish(d):
        state["done"] = d
        kind = d.get("kind")
        bar.config(value=100)
        pct.config(text="100%")
        if kind == "updated":
            stage.config(text="更新完成")
            info["status"].config(text="已自动更新 v%s → v%s，正在准备启动登录器"
                                  % (d.get("from_v"), d.get("v")), fg=C_GREEN)
            append("更新成功：v%s -> v%s" % (d.get("from_v"), d.get("v")))
        elif kind == "latest":
            stage.config(text="已是最新版")
            info["status"].config(text="本地 v%s 已是最新，无需下载" % d.get("v"), fg=C_GREEN)
            append("网盘文件与本地一致（id=%s），无需更新。" % d.get("id"))
        elif kind == "same":
            stage.config(text="已是最新版")
            info["status"].config(text="网盘与本地内容一致，无需替换", fg=C_GREEN)
            append("下载校验后确认内容与本地完全一致，未替换。")
        b_cancel.config(state="normal")
        b_launch.config(state="normal")
        b_retry.config(state="disabled")
        cancel_countdown()
        auto_left[0] = AUTO_SECONDS
        countdown.config(text="%d 秒后启动登录器并退出（点\"取消\"可留下）" % AUTO_SECONDS)
        auto_job[0] = root.after(1000, tick_countdown)

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "set":
                    v, s = payload
                    bar.config(value=v)
                    pct.config(text="%d%%" % v)
                    if s:
                        stage.config(text=s)
                elif kind == "log":
                    append(payload)
                elif kind == "info":
                    append(payload)
                elif kind == "done":
                    append(payload if isinstance(payload, str) else "完成")
                    if not isinstance(payload, str):
                        finish(payload)
                    else:
                        state["done"] = {"kind": "latest"}
                        finish(state["done"])
                elif kind == "error":
                    stage.config(text="出错了")
                    info["status"].config(text=payload, fg=C_RED)
                    append("错误：" + payload)
                    b_launch.config(state="normal")
                    b_retry.config(state="normal")
                    b_cancel.config(state="normal")
                elif kind == "quit":
                    pass
        except queue.Empty:
            pass
        root.after(120, poll)

    def stop_and_ask():
        if state["done"] is not None:
            cancel_countdown()
            countdown.config(text="已取消自动启动，可点\"启动登录器\"或\"关闭\"。")
            b_launch.config(state="normal")
            return
        stop_flag.set()
        append("已请求取消…")

    b_cancel.config(command=stop_and_ask)
    b_retry.config(command=start)
    b_launch.config(command=launch_and_close)
    b_close.config(command=lambda: (stop_flag.set(), root.destroy()))
    root.protocol("WM_DELETE_WINDOW", lambda: (stop_flag.set(), root.destroy()))

    # 远程信息填卡
    def refresh_remote(d):
        pass

    def hook_done_for_card():
        while True:
            break

    # 更新信息卡（在 done 时补充）
    _orig_finish = finish

    def finish2(d):
        if isinstance(d, dict):
            if d.get("host"):
                info["remote"].config(text="%s / id=%s" % (d["host"], d.get("id")))
        _orig_finish(d)

    finish = finish2
    root.bind("<Return>", lambda e: start())
    append("打开窗口后自动开始检查；若网盘有新版会自动下载替换。")
    start()
    poll()
    root.mainloop()


def _exc_hook(exc_type, exc, tb):
    try:
        path = os.path.join(U.UPDATER_HOME, "updater_error.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] "))
            traceback.print_exception(exc_type, exc, tb, file=f)
    except Exception:
        pass


def main():
    if "--check-only" in sys.argv:
        st = U.load_state()
        print("game dir:", U.LAUNCHER_DIR, "|", U.GAME_DIR_WHY)
        print("launcher :", U.LAUNCHER_EXE)
        print("local v  :", U.exe_required_version(U.LAUNCHER_EXE))
        print("state    :", st.get("last_file_id"), (st.get("installed") or {}).get("sha256"))
        return
    try:
        gui()
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
