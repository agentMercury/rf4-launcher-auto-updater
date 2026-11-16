#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rf4_updater.py — RF4钓鱼登录器自动更新器（纯 Python 标准库，无第三方依赖）

用法:
  python rf4_updater.py status            查看本地/远端版本状态
  python rf4_updater.py check             检查网盘是否有新登录器（不下载）
  python rf4_updater.py update            检查 -> 下载 -> 校验 -> 替换（完整自动更新）
  python rf4_updater.py update --no-replace   只下载和校验，不替换
  python rf4_updater.py watch             常驻监控：轮询网盘 + 监听登录器"版本不匹配"弹窗日志，自动更新
  python rf4_updater.py install <file>    安装已下载好的 exe（校验后替换）

更新链路（全部实测跑通）:
  蓝奏云文件夹页 b0nyv844f -> filemoreajax.php 文件列表 -> 分享短链 -> /fn 页 ->
  ajaxfile.php downprocess -> 下载直链(30分钟时效) -> 阿里WAF(acw_sc__v2 纯算法) ->
  蓝奏云二次验证页(ajax.php, el=2, 延迟提交) -> 真实文件直链 -> 下载 -> sha256 校验 -> 事务替换
"""
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if sys.stdout is None:      # pythonw.exe 计划任务模式下无控制台
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# --------------------------- 网盘与路径常量 ---------------------------
UPDATER_HOME = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(UPDATER_HOME, "backups")
DOWNLOAD_DIR = os.path.join(UPDATER_HOME, "downloads")
UPDATER_STATE = os.path.join(UPDATER_HOME, "updater_state.json")
UPDATER_LOG = os.path.join(UPDATER_HOME, "updater.log")
LOG_DIR = os.path.expandvars(r"%APPDATA%\RF4LocalLauncher")   # 登录器运行日志（弹窗信号来源）

FOLDER_PATH = "/b0nyv844f"          # "登录插件下载" 文件夹
FOLDER_FID = "11479625"
FOLDER_UID = "1506996"
HOSTS = ["wwbxt.lanzoul.com", "wwbxt.lanzoui.com", "wwbxt.lanzn.com"]  # 主域+镜像域
TARGET_NAME = "RF4钓鱼登录器.exe"

POLL_INTERVAL = 30 * 60              # watch 模式轮询网盘间隔（秒）
GATE_DELAY = 6                       # 二次验证页拿到后等待秒数（实测需 >2s 页面加载动画）
VER_RE = rb"rf4-4\.0\.25034-lan-v(\d+)-required-launcher"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ----------------------------- 游戏目录定位 -----------------------------
def _is_game_dir(p):
    """判断目录是否为 RF4 游戏目录（依据：目录内有游戏本体 rf4_x64.exe）。"""
    try:
        return bool(p) and os.path.isfile(os.path.join(p, "rf4_x64.exe"))
    except OSError:
        return False


def _scan_parent():
    """只在 rf4-auto-update 的上一级范围内查找；不递归、不搜其他目录。

    优先判断"上一级目录本身就是游戏目录"（推荐用法：把 rf4-auto-update 整个
    放进游戏目录里），其次才在上一级目录的子目录里找。
    """
    home = UPDATER_HOME
    parent = os.path.dirname(home)
    if not parent or parent == home:
        return None, parent
    # 用法一（推荐）：rf4-auto-update 直接放在游戏目录里 → 上一级就是游戏目录
    if _is_game_dir(parent):
        return parent, parent
    best = None
    try:
        with os.scandir(parent) as it:
            for e in it:
                try:
                    if not e.is_dir() or not _is_game_dir(e.path):
                        continue
                except OSError:
                    continue
                # 已放过登录器的目录优先（就是正在使用的那份游戏）
                if os.path.isfile(os.path.join(e.path, TARGET_NAME)):
                    return e.path, parent
                if best is None:
                    best = e.path
    except OSError:
        pass
    return best, parent


def find_game_dir():
    r"""确定 RF4 游戏目录（登录器就下载/替换到这里）。

    查找顺序（严格限定范围，绝不跑到别的目录去搜索或写文件）：
      1. 环境变量 RF4_GAME_DIR（手工指定，最高优先）
      2. updater_state.json 里上次记住的 game_dir
      3. rf4-auto-update 的上一级目录：本身就是游戏目录优先，其次扫其子目录
      4. 登录器自身设置 %APPDATA%\RF4LocalLauncher\settings.json 的 gameDir
    """
    env = os.environ.get("RF4_GAME_DIR")
    if _is_game_dir(env):
        return os.path.abspath(env), "环境变量 RF4_GAME_DIR"
    try:
        st = json.load(open(UPDATER_STATE, encoding="utf-8"))
        remembered = st.get("game_dir")
        if _is_game_dir(remembered):
            return os.path.abspath(remembered), "上次记住的位置"
    except Exception:
        pass
    found, parent = _scan_parent()
    if found:
        if os.path.abspath(found) == os.path.abspath(parent):
            return found, "上一级目录即游戏目录"
        return found, "上一级目录扫描 " + parent
    try:
        j = json.load(open(os.path.join(LOG_DIR, "settings.json"), encoding="utf-8"))
        gd = j.get("gameDir")
        if _is_game_dir(gd):
            return os.path.abspath(gd), "登录器 settings.json"
    except Exception:
        pass
    raise RuntimeError(
        "找不到 RF4 游戏目录（要能找到游戏本体 rf4_x64.exe）。请把 rf4-auto-update "
        "文件夹整个放进 RF4 游戏目录里（和 rf4_x64.exe 同一个目录），"
        "或设置环境变量 RF4_GAME_DIR 指向游戏目录。")


GAME_DIR, GAME_DIR_WHY = find_game_dir()
LAUNCHER_DIR = GAME_DIR
LAUNCHER_EXE = os.path.join(LAUNCHER_DIR, TARGET_NAME)
STATE_FILE = os.path.join(LAUNCHER_DIR, ".rf4-local", "state.json")

# --------------------------- WAF 求解器 ---------------------------
# acw_sc__v2 = hexxor(reorder(arg1, M), P)
M = [15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13, 6, 11,
     39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34, 37, 12, 36]
P = "3000176000856006061501533003690027800375"


def solve_acw(arg1: str) -> str:
    q = [""] * 40
    for x, y in enumerate(arg1[:40]):
        for z, mv in enumerate(M):
            if mv == x + 1:
                q[z] = y
    u = "".join(q)
    out = []
    for i in range(0, min(len(u), len(P)), 2):
        out.append("%02x" % (int(u[i:i + 2], 16) ^ int(P[i:i + 2], 16)))
    return "".join(out)


# ---------------------------- HTTP 层 ----------------------------
def maybe_gunzip(b: bytes) -> bytes:
    if b[:2] == b"\x1f\x8b":
        try:
            return __import__("gzip").decompress(b)
        except Exception:
            try:
                return __import__("gzip").decompressobj().decompress(b)
            except Exception:
                return b
    return b


class Session:
    """带 cookie 的会话；自动解 acw_sc__v2 WAF 挑战并重试。"""

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA)]

    def _rq(self, url, referer=None, data=None, headers=None):
        r = urllib.request.Request(
            url, data=urllib.parse.urlencode(data).encode() if data else None)
        if referer:
            r.add_header("Referer", referer)
        if data:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        return r

    def _set_cookie(self, host, name, value):
        self.cj.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=host, domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True, secure=False,
            expires=int(time.time() + 3600), discard=False,
            comment=None, comment_url=None, rest={}))

    def fetch(self, url, referer=None, data=None, headers=None,
              maxlen=400_000, timeout=90, tries=5):
        """GET/POST，返回 (resp, body_bytes)。自动过 WAF。"""
        for _ in range(tries):
            resp = self.op.open(self._rq(url, referer, data, headers), timeout=timeout)
            body = maybe_gunzip(resp.read(maxlen))
            txt = body.decode("utf-8", "replace")
            if "var arg1=" in txt and "acw_sc__v2" in txt:
                arg1 = re.search(r"var arg1='([0-9A-Fa-f]+)'", txt).group(1)
                host = urllib.parse.urlsplit(resp.geturl()).hostname
                self._set_cookie(host, "acw_sc__v2", solve_acw(arg1))
                continue
            return resp, body
        raise RuntimeError("WAF 循环未通过: " + url)

    def text(self, url, **kw):
        kw.setdefault("maxlen", 400_000)
        return self.fetch(url, **kw)[1].decode("utf-8", "replace")


# ---------------------------- 工具函数 ----------------------------
def log(msg: str):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(UPDATER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def exe_required_version(path):
    """扫描 exe 内嵌的 rf4-4.0.25034-lan-vN-required-launcher，取最大 N。"""
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    vs = [int(m.group(1)) for m in re.finditer(VER_RE, data)]
    return max(vs) if vs else None


def load_state():
    try:
        return json.load(open(UPDATER_STATE, encoding="utf-8"))
    except Exception:
        return {"seen_logs": [], "last_file_id": None, "installed": None}


def save_state(st):
    st["game_dir"] = LAUNCHER_DIR          # 记住游戏目录，搬家后仍能定位
    st["game_dir_how"] = GAME_DIR_WHY
    with open(UPDATER_STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# ---------------------------- 蓝奏云链路 ----------------------------
class Lanzou:
    def __init__(self, session=None):
        self.s = session or Session()

    def list_files(self):
        """返回网盘文件夹内文件列表 [{'id','name_all','size','time'}]，带镜像重试。"""
        last = None
        for host in HOSTS:
            try:
                html = self.s.text("https://" + host + FOLDER_PATH,
                                   referer="https://www.kezongsyst.com/")
                # 蓝奏云会随机轮换混淆变量名，改为从 ajax data 块动态定位：
                # data 里有 't':<varName>, 'k':<varName> —— 先抓变量名再找 var 声明值。
                ref_t = re.search(r"'t'\s*:\s*([A-Za-z_$][\w$]*)", html)
                ref_k = re.search(r"'k'\s*:\s*([A-Za-z_$][\w$]*)", html)
                m_t = m_k = None
                if ref_t:
                    m_t = re.search(r"var\s+" + re.escape(ref_t.group(1)) +
                                    r"\s*=\s*'([^']+)'", html)
                if ref_k:
                    m_k = re.search(r"var\s+" + re.escape(ref_k.group(1)) +
                                    r"\s*=\s*'([^']+)'", html)
                if not (m_t and m_k):
                    # 兜底：按值的形态找（10位以上纯数字 / 32位hex）
                    nums = re.findall(r"var\s+(\w+)\s*=\s*'(\d{6,})'", html)
                    hexs = re.findall(r"var\s+(\w+)\s*=\s*'([a-f0-9]{32})'", html)
                    if nums and hexs:
                        m_t = type("M", (), {"group": lambda s, n, v=nums[0][1]: v})()
                        m_k = type("M", (), {"group": lambda s, n, v=hexs[0][1]: v})()
                if not (m_t and m_k):
                    last = "页面 token 解析失败(镜像域可能不可用): " + host
                    continue
                uid = re.search(r"'uid'\s*:\s*'([^']+)'", html)
                puid = re.search(r"'puid'\s*:\s*'([^']+)'", html)
                js = self.s.text("https://" + host + "/filemoreajax.php?file=" + FOLDER_FID,
                                 referer="https://" + host + FOLDER_PATH,
                                 data={"lx": 2, "fid": FOLDER_FID,
                                       "uid": uid.group(1) if uid else FOLDER_UID,
                                       "puid": puid.group(1) if puid else "",
                                       "pg": 1, "rep": 0,
                                       "t": m_t.group(1), "k": m_k.group(1), "up": 1},
                                 headers={"X-Requested-With": "XMLHttpRequest"})
                j = json.loads(js)
                if j.get("zt") == 1 and j.get("text"):
                    return host, j["text"]
                last = "列表返回异常: %s" % js[:120]
            except Exception as e:
                last = repr(e)[:160]
            self.s = Session()      # 换域重置会话
        raise RuntimeError("网盘列表获取失败: " + str(last))

    def download_url(self, host, file_id):
        """短链页 -> fn 页 -> downprocess，返回下载直链（30分钟时效）。"""
        sh = self.s.text("https://" + host + "/" + file_id,
                         referer="https://" + host + FOLDER_PATH)
        iframe = re.search(r'<iframe[^>]+src="([^"]+)"', sh).group(1)
        fn = self.s.text("https://" + host + iframe,
                         referer="https://" + host + "/" + file_id)
        wp = re.search(r"wp_sign\s*=\s*'([^']+)'", fn).group(1)
        m_ajax = re.search(r"ajaxdata\s*=\s*'([^']+)'", fn)
        ajax = m_ajax.group(1) if m_ajax else ""
        m1 = re.search(r"var domain1 = '([^']+)'", fn)
        m2 = re.search(r"var domain2 = '([^']+)'", fn)
        got = False
        for dom in filter(None, (m2.group(1) if m2 else None, m1.group(1) if m1 else None)):
            try:
                _, body = self.s.fetch(dom + "&time=0", referer="https://" + host + iframe,
                                       data={"action": "downprocess", "sign": wp,
                                             "websignkey": ajax, "signs": ajax,
                                             "websign": "", "kd": "1", "ves": 1},
                                       maxlen=4000)
                dp = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                continue
            if str(dp.get("zt")) == "1":
                got = True
                return dp["dom"] + "/file/" + dp["url"]
            last = dp
        raise RuntimeError("downprocess 失败: " + str(last))

    def real_file_url(self, dl):
        """下载直链 -> (可能)interstitial 二次验证 -> 真实文件 URL；返回 (resp, body)。"""
        referer = "https://" + HOSTS[0] + "/"
        resp, body = self.s.fetch(dl + "&toolsdown", referer=referer, maxlen=2_000_000)
        if body[:2] == b"MZ":
            return self.s.fetch(resp.geturl(), referer=referer, maxlen=1 << 30)
        txt = body.decode("utf-8", "replace")
        if "down_r" not in txt:
            raise RuntimeError("下载返回异常内容(前200): " + txt[:200])
        mfile = re.search(r"'file':'([A-Za-z0-9+/=]+)'", txt)
        msign = re.search(r"'sign':'([A-Za-z0-9+/=]+)'", txt)
        if not (mfile and msign):
            raise RuntimeError("验证页缺少 token")
        ajax_url = urllib.parse.urljoin(resp.geturl(), "ajax.php")
        origin = urllib.parse.urlsplit(resp.geturl())
        time.sleep(GATE_DELAY)
        _, body2 = self.s.fetch(ajax_url, referer=resp.geturl(),
                                data={"file": mfile.group(1), "el": "2",
                                      "sign": msign.group(1)},
                                headers={"X-Requested-With": "XMLHttpRequest",
                                         "Origin": origin.scheme + "://" + origin.netloc},
                                maxlen=6000)
        j = json.loads(body2.decode("utf-8", "replace"))
        if str(j.get("zt")) != "1" or not j.get("url") or "SignError" in str(j.get("url")):
            raise RuntimeError("二次验证失败: %s" % json.dumps(j, ensure_ascii=False)[:200])
        final = j["url"] if str(j["url"]).startswith("http") else urllib.parse.urljoin(ajax_url, j["url"])
        return self.s.fetch(final, referer=resp.geturl(), maxlen=1 << 30)

    def download_to(self, dest_path):
        """完整链路下载一个文件到 dest_path，返回元数据 dict。"""
        host, files = self.list_files()
        f = next((x for x in files if x["name_all"] == TARGET_NAME), files[0])
        log("网盘文件: id=%s size=%s time=%s host=%s" % (f["id"], f["size"], f["time"], host))
        dl = self.download_url(host, f["id"])
        log("下载直链: %s..." % dl[:90])
        resp, body = self.real_file_url(dl)
        if body[:2] != b"MZ":
            raise RuntimeError("下载内容不是 PE 文件")
        tmp = dest_path + ".part"
        with open(tmp, "wb") as fp:
            fp.write(body)
        os.replace(tmp, dest_path)
        return {"file_id": f["id"], "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "list_size": f["size"], "list_time": f["time"]}


# ---------------------------- 触发信号 ----------------------------
def mismatch_events(state):
    """扫描登录器运行日志，找出新的『版本不匹配』弹窗记录。"""
    seen = set(state.get("seen_logs", []))
    fresh = []
    try:
        names = sorted(os.listdir(LOG_DIR))
    except OSError:
        return fresh
    for n in names:
        if not (n.startswith("launcher-") and n.endswith(".json")) or n in seen:
            continue
        seen.add(n)
        try:
            d = json.load(open(os.path.join(LOG_DIR, n), encoding="utf-8"))
        except Exception:
            continue
        err = d.get("Error") or ""
        if d.get("ExitStatus") == 1 and ("版本不匹配" in err or "网盘" in err):
            fresh.append(n)
    state["seen_logs"] = list(seen)[-500:]
    return fresh


# ---------------------------- 安装事务 ----------------------------
def install(new_path, dry=False):
    """备份当前登录器 -> 移走旧的(运行中可改名) -> 放入新的 -> 失败回滚。

    游戏目录里没有登录器时（首次安装/被删），直接把新文件放进去。
    """
    if not os.path.exists(LAUNCHER_EXE):
        if dry:
            print("DRY-RUN: 游戏目录无登录器，将直接安装新文件 -> " + LAUNCHER_EXE)
            return "dry"
        os.makedirs(LAUNCHER_DIR, exist_ok=True)
        shutil.move(new_path, LAUNCHER_EXE)
        log("游戏目录原本没有登录器，已直接安装: %s" % LAUNCHER_EXE)
        return "installed"
    old_sha = sha256_file(LAUNCHER_EXE)
    new_sha = sha256_file(new_path)
    if old_sha == new_sha:
        log("本地已是最新版，无需替换 (%s)" % new_sha[:16])
        return "same"
    v_old, v_new = exe_required_version(LAUNCHER_EXE), exe_required_version(new_path)
    if v_old is not None and v_new is not None and v_new < v_old:
        raise RuntimeError("远端版本 v%s 低于本地 v%s，拒绝降级" % (v_new, v_old))
    if dry:
        print("DRY-RUN: 将执行 v%s -> v%s 替换" % (v_old, v_new))
        return "dry"
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, "RF4钓鱼登录器.v%s.%s.bak.exe" % (v_old, ts))
    shutil.copy2(LAUNCHER_EXE, backup)
    log("已备份 -> %s" % backup)
    stale = LAUNCHER_EXE + ".old"
    if os.path.exists(stale):
        try:
            os.remove(stale)
        except OSError:
            pass
    try:
        os.rename(LAUNCHER_EXE, stale)          # Windows 允许改名运行中的 exe
    except OSError as e:
        raise RuntimeError("无法移走旧登录器（正在使用？）: %r" % e)
    try:
        shutil.move(new_path, LAUNCHER_EXE)
    except Exception as e:
        os.replace(stale, LAUNCHER_EXE)          # 回滚
        raise RuntimeError("写入新登录器失败，已回滚: %r" % e)
    try:
        os.remove(stale)
    except OSError:
        pass                                     # 旧进程未退出时 .old 稍后自然可删
    log("替换完成: sha256 %s -> %s, v%s -> v%s"
        % (old_sha[:16], sha256_file(LAUNCHER_EXE)[:16], v_old, v_new))
    return "updated"


# ---------------------------- 命令 ----------------------------
def backup_usage():
    """返回 (备份文件数, 总字节数)。只统计 backups/ 下的文件，忽略目录不存在的情况。"""
    if not os.path.isdir(BACKUP_DIR):
        return 0, 0
    cnt = 0
    total = 0
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        try:
            if os.path.isfile(path):
                cnt += 1
                total += os.path.getsize(path)
        except OSError:
            continue
    return cnt, total


def cmd_status():
    v = exe_required_version(LAUNCHER_EXE)
    st = load_state()
    print("登录器      : %s" % LAUNCHER_EXE)
    print("本地大小    : %s bytes" % (os.path.getsize(LAUNCHER_EXE) if os.path.exists(LAUNCHER_EXE) else "缺失"))
    print("本地要求版本: v%s" % v)
    if os.path.exists(STATE_FILE):
        try:
            j = json.load(open(STATE_FILE, encoding="utf-8"))
            print("补丁要求版本: %s" % j.get("version"))
            req = re.search(r"-v(\d+)-", j.get("version") or "")
            if req and v is not None:
                need = int(req.group(1))
                print("匹配状态    : %s" % ("OK 可正常启动" if v >= need else "落后！下次启动会弹窗"))
        except Exception as e:
            print("state.json 读取失败:", e)
    print("上次网盘文件: %s" % st.get("last_file_id"))
    if st.get("installed"):
        print("上次自动更新: %s" % json.dumps(st["installed"], ensure_ascii=False))
    cnt, total = backup_usage()
    if cnt:
        print("备份占用    : %d 个文件，%.1f MB（%s）" % (cnt, total / 1048576.0, BACKUP_DIR))
        print("              不再需要旧版时可删除，保留最近一个即可回滚")
    else:
        print("备份占用    : 无备份（尚未替换过登录器）")


def cmd_check(quiet=False):
    lz = Lanzou()
    host, files = lz.list_files()
    f = next((x for x in files if x["name_all"] == TARGET_NAME), files[0])
    st = load_state()
    changed = f["id"] != st.get("last_file_id")
    if not quiet:
        print("远端文件: id=%s size=%s time=%s" % (f["id"], f["size"], f["time"]))
        print("记录 id : %s" % st.get("last_file_id"))
        print("结论    : %s" % ("网盘有新版（可执行 update）" if changed else "网盘文件未变化"))
    return changed, f


def cmd_update(no_replace=False):
    st = load_state()
    v_local = exe_required_version(LAUNCHER_EXE)
    log("开始更新流程（本地 v%s）" % v_local)
    last_err = None
    for attempt in range(1, 4):
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            lz = Lanzou()
            meta = lz.download_to(os.path.join(DOWNLOAD_DIR, TARGET_NAME))
            log("下载完成: %s bytes sha256=%s" % (meta["size"], meta["sha256"]))
            v_new = exe_required_version(os.path.join(DOWNLOAD_DIR, TARGET_NAME))
            log("远端内嵌要求版本: v%s" % v_new)
            if v_local is not None and v_new is not None and v_new <= v_local \
                    and meta["sha256"] == sha256_file(LAUNCHER_EXE):
                log("远端与本地完全一致，无需替换")
                st["last_file_id"] = meta["file_id"]
                st["installed"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "to_v": v_new, "sha256": meta["sha256"],
                                   "size": meta["size"], "result": "same"}
                save_state(st)
                return "same"
            if no_replace:
                print("已下载到 downloads/%s (v%s)，--no-replace 未替换" % (TARGET_NAME, v_new))
                return "downloaded"
            res = install(os.path.join(DOWNLOAD_DIR, TARGET_NAME))
            st["last_file_id"] = meta["file_id"]
            st["installed"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "from_v": v_local, "to_v": v_new, **meta}
            save_state(st)
            log("更新完成: %s (%s)" % (res, "已替换" if res == "updated" else res))
            return res
        except Exception as e:
            last_err = e
            log("第 %d 次尝试失败: %r" % (attempt, e))
            time.sleep(5 * attempt)
    raise SystemExit("更新失败: %r" % last_err)


def recent_update_ok(st, window=3600):
    """最近 window 秒内刚成功更新过 -> 跳过，防止失败循环。"""
    inst = st.get("installed") or {}
    at = inst.get("at")
    if not at:
        return False
    try:
        t = time.mktime(time.strptime(at, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    return (time.time() - t) < window


def cmd_new_mismatch():
    """给计划任务用：只在发现新的版本不匹配日志且不在冷却期时更新。"""
    st = load_state()
    events = mismatch_events(st)
    save_state(st)          # 无论走哪个分支，已读日志都要落盘
    if not events:
        print("no-new-mismatch")
        return 0
    if recent_update_ok(st):
        log("发现 %d 条不匹配日志，但 1 小时内刚更新过，跳过" % len(events))
        print("cooldown")
        return 0
    log("发现新的版本不匹配日志: %s -> 执行更新" % ",".join(events[:3]))
    cmd_update()
    return 0


def cmd_watch():
    st = load_state()
    # 首次运行不追溯历史日志，只处理新产生的
    try:
        st["seen_logs"] = [n for n in os.listdir(LOG_DIR)
                           if n.startswith("launcher-") and n.endswith(".json")]
    except OSError:
        pass
    save_state(st)
    log("监控启动: 日志扫描每 30 秒，网盘轮询每 %d 秒。日志目录 %s" % (POLL_INTERVAL, LOG_DIR))
    next_remote = 0.0
    while True:
        try:
            st = load_state()
            changed = False
            events = mismatch_events(st)
            if events:
                save_state(st)
                log("发现 %d 条新的『版本不匹配』弹窗日志 -> 立即更新" % len(events))
                changed = True
            elif time.time() >= next_remote:
                next_remote = time.time() + POLL_INTERVAL
                c, _ = cmd_check(quiet=True)
                save_state(st)
                if c:
                    log("网盘文件 id 变化 -> 立即更新")
                    changed = True
            if changed:
                cmd_update()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log("监控循环异常（继续）: %r" % e)
        time.sleep(30)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "status":
        cmd_status()
    elif cmd == "check":
        cmd_check()
    elif cmd == "update":
        cmd_update(no_replace="--no-replace" in args)
    elif cmd == "watch":
        cmd_watch()
    elif cmd == "new-mismatch":
        sys.exit(cmd_new_mismatch())
    elif cmd == "install" and len(args) > 1:
        res = install(args[1])
        st = load_state()
        st["installed"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "result": res}
        save_state(st)
        print(res)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
