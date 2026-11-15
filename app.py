# -*- coding: utf-8 -*-
"""Bonfire · 篝火（无边框原生小窗播放器）
- 窗口：pywebview(WebView2) 无边框小窗，尺寸随页面内容自适应
- 置顶：Win32 SetWindowPos，按钮 -> 本地端口 -> 窗口层级
- 拖动：页面把鼠标按下转成"拖标题栏"，调用系统原生拖拽
- 音源：酷狗直链要 md5(hash+kgcloudv2)，这里算好交给页面
- 推歌：/push?q=歌名（豆包侧调用），页面轮询 /next 取指令
"""
import ctypes
import functools
import hashlib
import http.server
import html
import json
import os
import re
import shutil
import sys
import subprocess
import socket
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from ctypes import wintypes

APP_TITLE = "Bonfire"
APP_VER = "1.1.2"
UPDATE_API = "https://api.github.com/repos/aishangniwwww/bonfire-player/releases/latest"
WIN_W = 300
WIN_H = 312

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "FilmMusicWindow")
DL_CONF = os.path.join(DATA_DIR, "dl_dir.txt")
BG_DIR = os.path.join(DATA_DIR, "bg")
CACHE_DIR = DATA_DIR
DRAG = {"on": False}


def cache_size(p):
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total


def cache_info():
    bg = cache_size(BG_DIR)
    logn = 0
    try:
        logn = os.path.getsize(API_LOG)
    except Exception:
        pass
    return {"ok": True, "dir": CACHE_DIR, "bg": bg, "log": logn, "total": bg + logn}


def set_cache_dir(p):
    """缓存（背景媒体 / 日志）落盘位置，可换盘；换了就搬走旧的背景"""
    global CACHE_DIR, BG_DIR
    p = (p or "").strip().strip('"')
    if not p:
        return {"ok": False, "why": "empty"}
    try:
        os.makedirs(p, exist_ok=True)
    except Exception as e:
        return {"ok": False, "why": repr(e)[:80]}
    old = BG_DIR
    CACHE_DIR = p
    BG_DIR = os.path.join(p, "bg")
    try:
        os.makedirs(BG_DIR, exist_ok=True)
    except Exception as e:
        return {"ok": False, "why": repr(e)[:80]}
    moved = 0
    if os.path.isdir(old) and os.path.abspath(old) != os.path.abspath(BG_DIR):
        for name in ("bg.bin", "bg.json"):
            src = os.path.join(old, name)
            if os.path.isfile(src):
                try:
                    import shutil
                    shutil.copy2(src, os.path.join(BG_DIR, name))
                    moved += 1
                except Exception:
                    pass
    st = load_state()
    st["cache_dir"] = p
    save_state(st)
    log_api("cache dir -> %s (moved %s)" % (p, moved))
    return {"ok": True, "dir": CACHE_DIR, "moved": moved}


def cache_clear():
    """清背景媒体缓存 + 内存里的酷狗直链缓存 + 截断日志"""
    freed = 0
    for name in ("bg.bin", "bg.json"):
        p = os.path.join(BG_DIR, name)
        if os.path.isfile(p):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
            except Exception:
                pass
    KG_CACHE.clear()
    try:
        if os.path.isfile(API_LOG):
            freed += os.path.getsize(API_LOG)
            open(API_LOG, "w", encoding="utf-8").close()
    except Exception:
        pass
    log_api("cache cleared %s bytes" % freed)
    return {"ok": True, "freed": freed, "dir": CACHE_DIR}
DL_LOCK = threading.Lock()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def dl_dir():
    """下载目录：用户自己选过的优先，其次 D 盘，再其次系统下载夹"""
    try:
        with open(DL_CONF, "r", encoding="utf-8") as f:
            p = (f.read() or "").strip().strip('"')
        if p:
            os.makedirs(p, exist_ok=True)
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    for c in (r"D:\音乐下载", os.path.join(os.environ.get("USERPROFILE", ""), "Downloads", "Bonfire")):
        try:
            if not os.path.isdir(c):
                os.makedirs(c, exist_ok=True)
            if os.path.isdir(c):
                return c
        except Exception:
            continue
    return DATA_DIR


def pick_folder(title="选择下载目录"):
    """Win32 目录选择框（不依赖 tkinter / Qt，独立线程里调用不卡界面）"""
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32

        class BI(ctypes.Structure):
            _fields_ = [("hwndOwner", ctypes.c_void_p), ("pidlRoot", ctypes.c_void_p),
                        ("pszDisplayName", ctypes.c_wchar_p), ("lpszTitle", ctypes.c_wchar_p),
                        ("ulFlags", ctypes.c_uint), ("lpfn", ctypes.c_void_p),
                        ("lParam", ctypes.c_void_p), ("iImage", ctypes.c_int)]

        disp = ctypes.create_unicode_buffer(520)
        bi = BI()
        bi.pszDisplayName = ctypes.cast(disp, ctypes.c_wchar_p)
        bi.lpszTitle = title
        bi.ulFlags = 0x0001 | 0x0040          # RETURNONLYFSDIRS | NEWDIALOGSTYLE
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [ctypes.c_void_p]
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        path = ctypes.create_unicode_buffer(520)
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        shell32.SHGetPathFromIDListW(pidl, path)
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree(pidl)
        return path.value or ""
    except Exception:
        return ""


def set_dl_dir(p):
    p = (p or "").strip().strip('"')
    if not p:
        return ""
    try:
        p = os.path.expandvars(os.path.expanduser(p))
        os.makedirs(p, exist_ok=True)
        if not os.path.isdir(p):
            return ""
    except Exception:
        return ""
    try:
        with open(DL_CONF, "w", encoding="utf-8") as f:
            f.write(p)
    except Exception:
        return ""
    return p


LOCAL_DB = os.path.join(DATA_DIR, "local.json")
LOCAL_MUSIC = os.path.join(DATA_DIR, "music")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".mp4", ".m4v")


def local_load():
    try:
        with open(LOCAL_DB, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def local_save(rows):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOCAL_DB, "w", encoding="utf-8") as f:
            json.dump(rows[:500], f, ensure_ascii=False)
    except Exception:
        pass


LOCAL_COVERS = os.path.join(DATA_DIR, "covers")


def local_cover_dir():
    try:
        os.makedirs(LOCAL_COVERS, exist_ok=True)
    except Exception:
        pass
    return LOCAL_COVERS


def local_cover_save(lid, url):
    """把搜到的专辑封面落成本地缓存文件，返回给页面用的相对地址"""
    url = (url or "").strip()
    if not url or lid <= 0:
        return ""
    try:
        d = local_cover_dir()
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Referer": "https://y.qq.com/"})
        with urllib.request.urlopen(req, timeout=14) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") or "").lower()
        if not data or len(data) > 8 * 1024 * 1024:
            return ""
        ext = ".jpg"
        if data[:8] == b"\x89PNG\r\n\x1a\n" or "png" in ctype:
            ext = ".png"
        elif data[:4] == b"RIFF" or "webp" in ctype:
            ext = ".webp"
        elif data[:3] == b"GIF":
            ext = ".gif"
        for old in os.listdir(d):
            if old.split(".")[0] == str(lid):
                try:
                    os.remove(os.path.join(d, old))
                except Exception:
                    pass
        with open(os.path.join(d, "%d%s" % (lid, ext)), "wb") as f:
            f.write(data)
        return "/local/coverfile?id=%d&v=%d" % (lid, int(time.time()))
    except Exception as e:
        log_api("local cover fail %r" % e)
        return url


def pick_audio_files():
    """系统多选文件框：只要音频"""
    try:
        user32 = ctypes.windll.user32
        comdlg32 = ctypes.windll.comdlg32
        OFN_ALLOWMULTISELECT = 0x00000200
        OFN_EXPLORER = 0x00080000
        OFN_FILEMUSTEXIST = 0x00001000
        OFN_PATHMUSTEXIST = 0x00000800

        class OPENFILENAME(ctypes.Structure):
            _fields_ = [("lStructSize", ctypes.c_uint), ("hwndOwner", wintypes.HWND),
                        ("hInstance", ctypes.c_void_p), ("lpstrFilter", wintypes.LPCWSTR),
                        ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", ctypes.c_uint),
                        ("nFilterIndex", ctypes.c_uint), ("lpstrFile", wintypes.LPWSTR),
                        ("nMaxFile", ctypes.c_uint), ("lpstrFileTitle", wintypes.LPWSTR),
                        ("nMaxFileTitle", ctypes.c_uint), ("lpstrInitialDir", wintypes.LPCWSTR),
                        ("lpstrTitle", wintypes.LPCWSTR), ("Flags", ctypes.c_uint),
                        ("nFileOffset", ctypes.c_ushort), ("nFileExtension", ctypes.c_ushort),
                        ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                        ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR)]

        buf = ctypes.create_unicode_buffer(8000)
        of = OPENFILENAME()
        of.lStructSize = ctypes.sizeof(OPENFILENAME)
        of.hwndOwner = find_hwnd()
        of.lpstrFilter = "音频\0" + ";".join("*" + e for e in AUDIO_EXT) + "\0所有文件\0*.*\0\0"
        of.nFilterIndex = 1
        of.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        of.nMaxFile = 8000
        of.lpstrTitle = "选择要导入 Bonfire 的歌曲"
        of.Flags = OFN_ALLOWMULTISELECT | OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
        if not comdlg32.GetOpenFileNameW(ctypes.byref(of)):
            return []
        raw = buf.value
        parts = raw.split("\0")
        head = parts[0]
        files = [p for p in parts[1:] if p]
        if not files:
            return [head] if os.path.isfile(head) else []
        return [os.path.join(head, f) for f in files]
    except Exception as e:
        log_api("pick_audio_files EXC %r" % e)
        return []


def ver_tuple(v):
    """把 1.1.0 / v1.2 这类版本串变成可比较的三元组"""
    nums = re.findall(r"\d+", str(v or ""))
    nums = [int(x) for x in nums[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def update_check():
    """问 GitHub 要最新 Release，跟本机版本比一比"""
    try:
        req = urllib.request.Request(UPDATE_API, headers={
            "User-Agent": "Bonfire/%s" % APP_VER,
            "Accept": "application/vnd.github+json",
        })
        blob = urllib.request.urlopen(req, timeout=10).read()
        d = json.loads(blob.decode("utf-8", "replace"))
    except Exception as e:
        log_api("update check fail %r" % e)
        return {"ok": False, "why": "连不上 GitHub：%s" % e, "cur": APP_VER}
    latest = str(d.get("tag_name") or "").lstrip("vV")
    url, size = "", 0
    assets = d.get("assets") or []
    for a in assets:                                  # 优先拿安装包那份 zip
        nm = str(a.get("name") or "")
        if nm.lower().endswith(".zip") and "setup" in nm.lower():
            url = a.get("browser_download_url") or ""
            size = int(a.get("size") or 0)
            break
    if not url:
        for a in assets:
            if str(a.get("name") or "").lower().endswith(".zip"):
                url = a.get("browser_download_url") or ""
                size = int(a.get("size") or 0)
                break
    newer = bool(latest) and ver_tuple(latest) > ver_tuple(APP_VER)
    return {"ok": True, "cur": APP_VER, "latest": latest or APP_VER, "newer": newer,
            "url": url, "size": size, "notes": d.get("body") or "",
            "page": d.get("html_url") or ""}


def update_install_dir():
    """当前程序装在哪个目录：升级就装回原地，别搬家"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def update_run(url):
    """下载新版安装包 → 解出安装程序 → 静默装回原目录并重启自己"""
    if not url:
        return {"ok": False, "why": "没有下载地址"}
    if not re.match(r"^https://", url):
        return {"ok": False, "why": "只认 https 地址"}
    if not re.search(r"github\.com|githubusercontent\.com", url):
        return {"ok": False, "why": "只从 GitHub 下载"}
    up_dir = os.path.join(DATA_DIR, "update")
    os.makedirs(up_dir, exist_ok=True)
    zpath = os.path.join(up_dir, "Bonfire-Setup.zip")
    # 国内直连 GitHub 下载常常先超时，多备两条镜像，谁快用谁
    mirrors = [url, "https://ghfast.top/" + url, "https://gh-proxy.com/" + url]
    last_err = ""
    got = False
    for u in mirrors:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Bonfire/%s" % APP_VER})
            with urllib.request.urlopen(req, timeout=45) as r:
                with open(zpath, "wb") as f:
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        if f.tell() > 64 * 1024 * 1024:
                            raise RuntimeError("安装包大得不对劲")
            if os.path.getsize(zpath) < 1024:
                raise RuntimeError("下载到的文件太小")
            got = True
            log_api("update downloaded via %s" % u.split("/")[2])
            break
        except Exception as e:
            last_err = str(e)
            log_api("update download fail via %s: %r" % (u.split("/")[2], e))
    if not got:
        return {"ok": False, "why": "下载失败：%s" % last_err}
    exe = os.path.join(up_dir, "Bonfire-Setup.exe")
    try:
        with zipfile.ZipFile(zpath) as z:
            hit = [n for n in z.namelist() if n.lower().endswith(".exe")]
            if not hit:
                return {"ok": False, "why": "安装包里没有 exe"}
            hit.sort(key=lambda n: 0 if "setup" in n.lower() or "安装" in n else 1)
            with z.open(hit[0]) as src, open(exe, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except Exception as e:
        log_api("update unzip fail %r" % e)
        return {"ok": False, "why": "解压失败：%s" % e}
    target = update_install_dir()

    def later():
        try:
            log_api("update run -> %s (target=%s)" % (exe, target))
            subprocess.Popen([exe, "--headless", target, "--no-desk", "--no-menu",
                              "--launch", "--quiet"],
                             creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        except Exception as e:
            log_api("update launch fail %r" % e)
            return
        time.sleep(0.4)
        os._exit(0)      # 自己先关门，别让安装程序撞上文件占用

    # 先把响应回给页面（页面要弹 toast），2 秒后再拉安装程序——
    # 安装程序第一步就 taskkill Bonfire，抢在前面回包才是安全顺序
    threading.Timer(2.0, later).start()
    return {"ok": True, "exe": exe, "dir": target}


def local_prune(rows=None):
    """把「文件已经不在了」的记录清掉：用户在外面删了歌，列表跟着自愈，
    免得下次导入同一首还被当成「已导入」"""
    if rows is None:
        rows = local_load()
    keep = []
    for r in rows:
        p = r.get("path") or ""
        if p and os.path.isfile(p):
            keep.append(r)
    if len(keep) != len(rows):
        log_api("local prune %d -> %d" % (len(rows), len(keep)))
        local_save(keep)
    return keep


def local_import(mode="copy"):
    """导入选中的音频：copy=复制进软件目录，link=只记原路径
    已经导入过、而且文件确实还在的才算重复；列表里那条记录文件早就丢了的，
    当作没导入过，直接把老记录修好重新加回来（以前会被判成「已导入」）"""
    got = pick_audio_files()
    if not got:
        return {"ok": False, "why": "未选择文件", "rows": local_load(), "added": 0}
    rows = local_prune()
    ids = [r.get("id", 0) for r in rows] or [0]
    nid = max(ids) + 1
    added = []
    dup = []
    failed = []
    healed = 0

    def alive_key(p):
        try:
            return os.path.normcase(os.path.abspath(p))
        except Exception:
            return ""

    live = {}
    for r in rows:
        p = r.get("path") or ""
        if p and os.path.isfile(p):
            live[alive_key(p)] = r

    if mode == "copy":
        os.makedirs(LOCAL_MUSIC, exist_ok=True)
    for path in got:
        if not os.path.isfile(path):
            failed.append({"name": os.path.basename(path), "why": "文件已经不在了"})
            continue
        try:
            if mode == "copy":
                name = os.path.basename(path)
                dst = os.path.join(LOCAL_MUSIC, name)
                if os.path.abspath(path) != os.path.abspath(dst):
                    if os.path.exists(dst):
                        stem, ext = os.path.splitext(name)
                        dst = os.path.join(LOCAL_MUSIC, "%s_%d%s" % (stem, nid, ext))
                    shutil.copy2(path, dst)
                real = dst
            else:
                real = path
            key = alive_key(real)
            hit = live.get(key)
            if hit is not None:
                dup.append({"id": hit.get("id"), "name": hit.get("name")})
                continue
            size = os.path.getsize(real) if os.path.exists(real) else 0
            # 列表里留着一条「文件已经没了」的同名同大小记录：复用它的 id，别再生一条
            stale = None
            for r in rows:
                p = r.get("path") or ""
                if p and not os.path.isfile(p):
                    if os.path.basename(p) == os.path.basename(real) and int(r.get("size") or 0) in (0, size):
                        stale = r
                        break
            if stale is not None:
                stale.update({"name": os.path.splitext(os.path.basename(real))[0],
                              "path": real, "size": size, "mode": mode,
                              "added": int(time.time())})
                live[key] = stale
                added.append(stale)
                healed += 1
                continue
            e = {"id": nid, "name": os.path.splitext(os.path.basename(real))[0],
                 "path": real, "size": size, "mode": mode, "added": int(time.time()),
                 "cover": ""}
            rows.append(e)
            live[key] = e
            added.append(e)
            nid += 1
        except Exception as ex:
            log_api("local_import skip %r" % ex)
            failed.append({"name": os.path.basename(path), "why": str(ex)[:80]})
    local_save(rows)
    log_api("local import mode=%s added=%d dup=%d healed=%d failed=%d"
            % (mode, len(added), len(dup), healed, len(failed)))
    return {"ok": True, "rows": rows, "added": added, "dup": dup,
            "failed": failed, "healed": healed}



def safe_name(t):
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (t or "").strip())
    return t[:96] or "track"


def guess_ext(url, fallback="mp3"):
    low = (url or "").lower().split("?")[0]
    for e in ("mp3", "m4a", "flac", "aac", "wav", "ogg", "mp4", "webm"):
        if low.endswith("." + e):
            return e
    return fallback


def http_proxy(u):
    """同源代理：把外部接口/歌词请求拉回本地，绕开跨域和代理站限流"""
    req = urllib.request.Request(u, headers={
        "User-Agent": UA,
        "Referer": "https://music.163.com/",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=12) as r:
        body = r.read()
        ctype = r.headers.get("Content-Type", "application/octet-stream")
    return body, ctype


def do_download(url, name, ext):
    ext = ext or guess_ext(url)
    fn = safe_name(name) + "." + ext
    folder = dl_dir()
    dst = os.path.join(folder, fn)
    base, n = dst, 1
    while os.path.exists(dst):
        dst = "%s (%d).%s" % (os.path.splitext(base)[0], n, ext)
        n += 1
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://music.163.com/",
    })
    with DL_LOCK:
        with urllib.request.urlopen(req, timeout=45) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            head = r.read(64)
            # 平台没版权时会塞回来一个 HTML 提示页，别把它当音频存下来
            if "text/html" in ctype or head[:1] == b"<" or head[:9].lower() == b"<!doctype":
                raise IOError("这版没给音频（多半没版权）")
            if len(head) < 32:
                raise IOError("返回内容太短")
            with open(dst, "wb") as f:
                f.write(head)
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
    if os.path.getsize(dst) < 4096:
        try:
            os.remove(dst)
        except Exception:
            pass
        raise IOError("文件太小，多半不是真音频")
    return dst
WEB = os.path.join(BASE, "web")
LOCAL = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "FilmMusicWindow")
STATE_FILE = os.path.join(LOCAL, "state.json")
PORT_FILE = os.path.join(LOCAL, "port.txt")
API_LOG = os.path.join(LOCAL, "api.log")
os.makedirs(LOCAL, exist_ok=True)

user32 = ctypes.windll.user32
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.ReleaseCapture.argtypes = []

MY_PID = {"v": os.getpid()}
WINDOW = {"w": None}


# ---------------- 状态 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(data):
    """合并写入：只覆盖传进来的键，别把别的设置（球贴边、置顶、圆角…）一起冲掉"""
    try:
        st = load_state()
        if not isinstance(st, dict):
            st = {}
        st.update(data or {})
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


# ---------------- 托盘 / 关闭行为 ----------------
TRAY = {"ok": False, "thread": None, "ni": None}
CLOSE_BG = {"on": True}          # 点叉叉 -> 藏进托盘继续播
HARD_EXIT = {"v": False}


def tray_bg():
    return bool(CLOSE_BG["on"])


def set_close_bg(on):
    CLOSE_BG["on"] = bool(on)
    st = load_state()
    st["close_bg"] = bool(on)
    save_state(st)
    log_api("close_bg=%s" % bool(on))


def hard_exit():
    """托盘「直接退出」/ 接口 /close：真的退干净"""
    HARD_EXIT["v"] = True
    try:
        if TRAY.get("ni") is not None:
            TRAY["ni"].Visible = False
    except Exception:
        pass
    try:
        WINDOW["w"].destroy()
    except Exception:
        pass
    time.sleep(0.3)
    os._exit(0)


def show_main():
    """托盘「显示主界面」"""
    try:
        w = WINDOW["w"]
        w.show()
        try:
            w.on_top = bool(load_state().get("on_top"))
        except Exception:
            pass
        log_api("tray -> show main")
        return True
    except Exception as e:
        log_api("tray show fail %r" % e)
        return False


def tray_start():
    def run():
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)   # STA
        except Exception:
            pass
        try:
            import clr
            clr.AddReference("System.Windows.Forms")
            clr.AddReference("System.Drawing")
            from System.Windows.Forms import (NotifyIcon, ContextMenuStrip,
                                              ToolStripMenuItem, Application, MouseButtons)
            from System.Drawing import Icon

            ico = None
            for p in (os.path.join(BASE, "app.ico"), os.path.join(BASE, "app.ico")):
                if os.path.exists(p):
                    try:
                        ico = Icon(p)
                        break
                    except Exception:
                        ico = None
            if ico is None:
                try:
                    ico = Icon.ExtractAssociatedIcon(sys.executable)
                except Exception:
                    ico = None

            ni = NotifyIcon()
            if ico is not None:
                ni.Icon = ico
            ni.Text = APP_TITLE[:60]
            ni.Visible = True

            menu = ContextMenuStrip()
            it_show = ToolStripMenuItem("显示主界面")
            it_exit = ToolStripMenuItem("直接退出")
            it_show.Click += lambda s, e: show_main()
            it_exit.Click += lambda s, e: hard_exit()
            menu.Items.Add(it_show)
            menu.Items.Add(it_exit)
            ni.ContextMenuStrip = menu

            def on_click(s, e):
                try:
                    if str(e.Button) == "Left":
                        show_main()
                except Exception:
                    pass
            ni.MouseClick += on_click

            TRAY["ni"] = ni
            TRAY["ok"] = True
            log_api("tray up")
            Application.Run()          # 该线程自带消息泵，托盘菜单靠它
        except Exception as e:
            TRAY["ok"] = False
            log_api("tray fail %r" % e)

    th = threading.Thread(target=run, daemon=True)
    TRAY["thread"] = th
    th.start()


# ---------------- 单实例 ----------------
MULTI = {"on": False}      # False = 只允许开一个
MUTEX = {"h": None}


def multi_on():
    return bool(MULTI["on"])


def set_multi(on):
    MULTI["on"] = bool(on)
    st = load_state()
    st["multi"] = bool(on)
    save_state(st)
    log_api("multi=%s" % bool(on))


def _ping_ports():
    """扫一遍本地端口，找还活着的 Bonfire，返回它的端口"""
    for p in (8777, 8778, 8779):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api" % p, timeout=0.7) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            if "Bonfire" in str(data.get("name") or ""):
                return p
        except Exception:
            continue
    return None


def wake_existing(port):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/tray/show" % port, timeout=2).read()
    except Exception:
        pass


def claim_single():
    """单开模式：已经有一个在跑，就把它的窗口叫出来，本进程直接退出"""
    if MULTI["on"]:
        return True
    k = ctypes.windll.kernel32
    h = k.CreateMutexW(None, False, "Bonfire_SingleInstance")
    MUTEX["h"] = h
    if not h:
        return True
    if k.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        p = _ping_ports()
        if p:
            wake_existing(p)
        return False
    p = _ping_ports()                    # 兜底：老进程用的是别的锁（比如旧名字）
    if p:
        wake_existing(p)
        return False
    return True


def watch_loop():
    """每 1.2s 盯一次窗口：球该贴边就贴边，主窗该回屏内就回屏内"""
    log_api("watch thread up")
    miss = 0
    tick = 0
    while True:
        try:
            time.sleep(1.2)
            if DRAG["on"]:
                continue
            hwnd = find_hwnd()
            if not hwnd:
                miss += 1
                if miss in (3, 20):
                    log_api("watch: no hwnd (#%d)" % miss)
                continue
            miss = 0
            tick += 1
            if tick == 1:
                log_api("watch: first tick opt=%s ball=%s" % (WOPT, BALL["on"]))
            if BALL["on"]:
                ball_snap(hwnd)
            else:
                keep_inside(hwnd)
                sync_round(hwnd)
        except Exception as e:
            log_api("watch err: %r" % e)


def log_api(msg):
    try:
        with open(API_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


_ST = load_state()
try:
    _cd = (_ST.get("cache_dir") or "").strip()
    if _cd and os.path.isdir(_cd):
        CACHE_DIR = _cd
        BG_DIR = os.path.join(_cd, "bg")
except Exception:
    pass
TOP = {"on": bool(_ST.get("on_top")), "hard": bool(_ST.get("top_hard"))}
WOPT = {"ball_snap": bool(_ST.get("ball_snap", True)),
        "win_keep": bool(_ST.get("win_keep", True)),
        "ball_top": bool(_ST.get("ball_top", True))}


def set_opt(k, v):
    if k not in WOPT:
        return None
    WOPT[k] = bool(v)
    if k == "ball_top" and BALL["on"]:
        raw_top(WOPT[k])
    st = load_state()
    st[k] = WOPT[k]
    save_state(st)
    log_api("opt %s -> %s" % (k, WOPT[k]))
    return WOPT[k]
PENDING = []
PENDING_LOCK = threading.Lock()
HB = {}
HB_LOCK = threading.Lock()
HB_COUNT = [0]
KG_CACHE = {}


# ---------------- Win32 ----------------
def _wins_for_pid(pid):
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            wp = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
            if wp.value != pid:
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if not n:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if APP_TITLE not in buf.value:
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if area < 1500:
                return True
            out.append((area, hwnd))
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    out.sort(key=lambda c: c[0], reverse=True)
    return out


def find_hwnd():
    c = _wins_for_pid(MY_PID["v"])
    return c[0][1] if c else None


def work_area(cx=None, cy=None, hwnd=None):
    """窗口所在显示器的工作区（去掉任务栏），返回 (l, t, r, b)"""
    try:
        MONITOR_DEFAULTTONEAREST = 2

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_long), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_long)]

        if cx is None or cy is None:
            r = RECT()
            user32.GetWindowRect(hwnd or find_hwnd(), ctypes.byref(r))
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        pt = wintypes.POINT(int(cx), int(cy))
        mon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        w = mi.rcWork
        return (w.left, w.top, w.right, w.bottom)
    except Exception:
        return None


def ball_snap(hwnd, margin=10):
    """球松手后贴到离它最近的屏幕边（左 / 右 / 上 / 下）"""
    if not WOPT["ball_snap"] or not hwnd:
        return False
    try:
        l, t, w, h = rect_of(hwnd)
        wa = work_area(l + w // 2, t + h // 2)
        if not wa:
            return False
        L_, T_, R_, B_ = wa
        cands = [(abs(l - L_), L_ + margin, t),
                 (abs((R_ - w - margin) - l), R_ - w - margin, t),
                 (abs(t - T_), l, T_ + margin),
                 (abs((B_ - h - margin) - t), l, B_ - h - margin)]
        cands.sort()
        _, nx, ny = cands[0]
        if nx == l and ny == t:
            return False
        user32.SetWindowPos(hwnd, 0, int(nx), int(ny), 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        log_api("ball snap -> %s,%s" % (nx, ny))
        return True
    except Exception:
        return False


def set_clipboard(text):
    """把文本塞进 Windows 剪贴板（不依赖浏览器权限）"""
    try:
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        GMEM_MOVEABLE = 0x0002
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u32.OpenClipboard.argtypes = [ctypes.c_void_p]
        u32.OpenClipboard.restype = ctypes.c_bool
        if not u32.OpenClipboard(None):
            return False
        try:
            u32.EmptyClipboard()
            buf = text.encode("utf-16-le")
            h = k32.GlobalAlloc(GMEM_MOVEABLE, len(buf) + 2)
            if not h:
                return False
            p = k32.GlobalLock(h)
            ctypes.memmove(p, buf, len(buf))
            k32.GlobalUnlock(h)
            ctypes.windll.user32.SetClipboardData(13, h)   # CF_UNICODETEXT
            return True
        finally:
            u32.CloseClipboard()
    except Exception:
        return False


def keep_inside(hwnd, margin=4):
    """窗口被拖 / 放大到屏幕外时，挪回最近的可视位置"""
    if not WOPT["win_keep"] or not hwnd:
        return False
    try:
        l, t, w, h = rect_of(hwnd)
        wa = work_area(l + w // 2, t + h // 2)
        if not wa:
            return False
        L_, T_, R_, B_ = wa
        nx = max(L_ + margin, min(R_ - w - margin, l))
        ny = max(T_ + margin, min(B_ - h - margin, t))
        if nx == l and ny == t:
            return False
        user32.SetWindowPos(hwnd, 0, int(nx), int(ny), 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        log_api("keep inside -> %s,%s (was %s,%s)" % (nx, ny, l, t))
        return True
    except Exception:
        return False


def rect_of(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def is_topmost(hwnd):
    if not hwnd:
        return None
    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def raw_top(on):
    """只改窗口的置顶标记，不动用户钉子的持久状态"""
    hwnd = find_hwnd()
    if not hwnd:
        return None
    user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE)
    return True


def apply_top(on):
    on = bool(on)
    hwnd = find_hwnd()
    if not hwnd:
        return None
    user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE)
    time.sleep(0.12)
    got = is_topmost(hwnd)
    TOP["on"] = on
    save_state({"on_top": on})
    log_api("set_top -> %s (hwnd_topmost=%s)" % (on, got))
    return got


def set_top_hard(on):
    """置顶最高：自己把窗口压到最前，但不去改「置顶」那个开关的状态。
    关闭时回到用户自己设的置顶状态。"""
    on = bool(on)
    TOP["hard"] = on
    save_state({"top_hard": on})
    if on:
        raw_top(True)
    elif not TOP["on"]:
        raw_top(False)
    log_api("set_top_hard -> %s (on_top=%s)" % (on, TOP["on"]))
    return on


def hard_top_loop():
    while True:
        time.sleep(1.0)
        if not TOP.get("hard"):
            continue
        try:
            hwnd = find_hwnd()
            if not hwnd or user32.IsIconic(hwnd):
                continue
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass


gdi32 = ctypes.windll.gdi32
gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
user32.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]


def polish_window(hwnd):
    """去掉 DWM 自己画的圆角 + 1px 边框，并把窗口样式收成纯 popup。
    球模式那圈"白框"就是 DWM 的合成描边，PrintWindow 抓不到、只有真实屏幕看得见。"""
    if not hwnd:
        return False
    try:
        dwm = ctypes.windll.dwmapi
        v = ctypes.c_int(1)                          # DWMWCP_DONOTROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(v), ctypes.sizeof(v))
        c = ctypes.c_uint(0xFFFFFFFE)                # DWMWA_COLOR_NONE
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(c), ctypes.sizeof(c))
        sh = ctypes.c_int(0)                         # DWMWA_NCRENDERING_POLICY=2 关掉系统描边
        dwm.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(sh), ctypes.sizeof(sh))
    except Exception:
        pass
    try:
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_SYSMENU = 0x00080000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        st = user32.GetWindowLongW(hwnd, -16)
        user32.SetWindowLongW(hwnd, -16,
                              st & ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU
                                     | WS_MINIMIZEBOX | WS_MAXIMIZEBOX))
    except Exception:
        pass
    try:
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x0002 | 0x0001 | 0x0004 | 0x0010 | 0x0020)  # +SWP_FRAMECHANGED
    except Exception:
        pass
    return True


ROUND = {"px": 0, "sig": None}
gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6


def _kill_frame_edges(hwnd):
    """DWM 会在无边框窗口外侧画一圈 1px 亮边；裁圆角后这圈白边特别显眼，重新按死"""
    try:
        dwm = ctypes.windll.dwmapi
        v = ctypes.c_int(1)                       # DWMWCP_DONOTROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(v), ctypes.sizeof(v))
        c = ctypes.c_uint(0xFFFFFFFE)             # DWMWA_COLOR_NONE
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(c), ctypes.sizeof(c))
        p = ctypes.c_int(0)                       # DWMWA_NCRENDERING_POLICY = NEVER
        dwm.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(p), ctypes.sizeof(p))
    except Exception:
        pass
    try:
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x0002 | 0x0001 | 0x0004 | 0x0020 | 0x0010)
    except Exception:
        pass


def set_round_win(hwnd, px):
    """圆润边框：整窗裁成圆角矩形，四角直接透出桌面"""
    if not hwnd:
        return False
    try:
        r = max(0, min(40, int(px or 0)))
        if not r:
            user32.SetWindowRgn(hwnd, None, True)
            ROUND["sig"] = None
            _kill_frame_edges(hwnd)
            return True
        _l, _t, w, h = rect_of(hwnd)
        r = max(2, min(r, min(w, h) // 3))
        rgn = gdi32.CreateRoundRectRgn(0, 0, int(w) + 1, int(h) + 1, r * 2, r * 2)
        if not rgn:
            return False
        user32.SetWindowRgn(hwnd, rgn, True)
        _kill_frame_edges(hwnd)
        ROUND["sig"] = (w, h, r)
        return True
    except Exception:
        return False


def sync_round(hwnd):
    if not ROUND["px"] or not hwnd or BALL["on"]:
        return False
    try:
        _l, _t, w, h = rect_of(hwnd)
    except Exception:
        return False
    r = max(2, min(int(ROUND["px"]), min(w, h) // 3))
    if ROUND["sig"] != (w, h, r):
        return set_round_win(hwnd, ROUND["px"])
    return False


def set_round_region(hwnd, size):
    """窗口裁成直径 size 的圆：球外那点底色直接不存在，桌面透出来"""
    try:
        if not size:
            user32.SetWindowRgn(hwnd, None, True)
            ROUND["sig"] = None
            return True
        rgn = gdi32.CreateEllipticRgn(0, 0, int(size), int(size))
        if not rgn:
            return False
        user32.SetWindowRgn(hwnd, rgn, True)
        return True
    except Exception:
        return False


def drag_window(click_check=False):
    """拖动窗口：按住鼠标期间后端 120Hz 跟着光标挪窗，比页面报增量顺得多。
    click_check=True（球模式）：松手后位移太小就当成"点了一下"，返回 "tap"。"""
    hwnd = find_hwnd()
    if not hwnd:
        return False
    DRAG["on"] = True
    try:
        before = rect_of(hwnd)
    except Exception:
        DRAG["on"] = False
        return False
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    off_x = pt.x - before[0]
    off_y = pt.y - before[1]
    VK_LBUTTON = 0x01
    last = (pt.x, pt.y)
    moved = 0
    t_end = time.time() + 30
    frames = 0
    while time.time() < t_end:
        if not (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000):
            break
        q = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(q))
        if q.x != last[0] or q.y != last[1]:
            moved += abs(q.x - last[0]) + abs(q.y - last[1])
            last = (q.x, q.y)
            user32.SetWindowPos(hwnd, 0, q.x - off_x, q.y - off_y, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            frames += 1
        time.sleep(0.008)
    try:
        after = rect_of(hwnd)
    except Exception:
        after = before
    dist = abs(after[0] - before[0]) + abs(after[1] - before[1])
    log_api("drag frames=%d dist=%d before=%s after=%s moved=%d"
            % (frames, dist, before[:2], after[:2], moved))
    if click_check and dist < 6:
        log_api("ball tap (page will restore)")
        DRAG["on"] = False
        return "tap"
    if click_check:
        ball_snap(hwnd)
    keep_inside(hwnd)
    DRAG["on"] = False
    return True


def move_by(dx, dy):
    hwnd = find_hwnd()
    if not hwnd:
        return None
    x, y, w, h = rect_of(hwnd)
    user32.SetWindowPos(hwnd, 0, x + int(dx), y + int(dy), 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER)
    return [x + int(dx), y + int(dy)]


# ---------------- 悬浮球：整窗缩成小球 / 还原 ----------------
BALL = {"on": False, "restore": None, "top_before": False}


def set_ball(on, css_h=0, css_w=0):
    """on=1 缩成 96x96 的小球，on=0 还原成小窗"""
    try:
        on = bool(int(on))
    except Exception:
        on = bool(on)
    hwnd = find_hwnd()
    if not hwnd:
        return None
    _x, _y, w, h = rect_of(hwnd)
    if on:
        if not BALL["on"]:
            BALL["restore"] = [w, h]
            BALL["top_before"] = TOP["on"]
        BALL["on"] = True
        ball_w = max(36, min(160, int(css_w) or 40))
        ball_h = max(36, min(160, int(css_h) or 40))
        user32.SetWindowPos(hwnd, 0, 0, 0, ball_w, ball_h, SWP_NOMOVE | SWP_NOZORDER)
        polish_window(hwnd)
        set_round_region(hwnd, ball_w)
        try:
            WINDOW["w"].resize(ball_w, ball_h)
        except Exception:
            pass
        BALL["on"] = True
        if WOPT["ball_top"] or TOP["on"]:
            raw_top(True)
        ball_snap(hwnd)
        log_api("ball on -> %sx%s top=%s" % (ball_w, ball_h, is_topmost(hwnd)))
        return [ball_w, ball_h]
    rest = BALL["restore"] or [284, 278]
    with HB_LOCK:
        inner_w = int(HB.get("w") or 0)
    delta = (rest[0] - inner_w) if inner_w else 0
    ch = int(css_h or 0)
    target_h = (ch + delta) if ch else rest[1]
    set_round_region(hwnd, 0)
    user32.SetWindowPos(hwnd, 0, 0, 0, int(rest[0]), max(160, min(1200, target_h)),
                        SWP_NOMOVE | SWP_NOZORDER)
    try:
        WINDOW["w"].resize(int(rest[0]), max(160, min(1200, target_h)))
    except Exception:
        pass
    BALL["on"] = False
    # 还原后立刻把圆润边框补回去：窗口底色是纯黑，区域一旦空着，
    # 四个角就会露出黑底（等 1.2s 的心跳再补就有一段黑框）
    try:
        ROUND["sig"] = None
        if ROUND["px"]:
            set_round_win(hwnd, ROUND["px"])
        else:
            _kill_frame_edges(hwnd)
    except Exception:
        pass
    raw_top(bool(TOP["on"] or TOP["hard"]))
    log_api("ball off -> %sx%s top=%s" % (rest[0], target_h, is_topmost(hwnd)))
    keep_inside(hwnd, margin=10)
    return [rest[0], target_h]


def resize_to(css_h):
    """页面报 CSS 内容高度；用宽度差补 DPI 缩放（高度心跳有滞后，不能用）"""
    if BALL["on"]:
        return None
    hwnd = find_hwnd()
    if not hwnd:
        return None
    with HB_LOCK:
        inner_w = int(HB.get("w") or 0)
    _x, _y, w, h = rect_of(hwnd)
    delta = (w - inner_w) if inner_w else 0
    target = max(180, min(1200, int(css_h) + delta))
    if h and abs(target - h) <= 2:
        return h
    user32.SetWindowPos(hwnd, 0, 0, 0, w, target, SWP_NOMOVE | SWP_NOZORDER)
    # 区域是按旧尺寸算的，改高度后必须重算，否则圆角边上会漏黑底
    sync_round(hwnd)
    log_api("resize %s -> %s (css %s)" % (h, target, css_h))
    return target



# ---------------- QQ 音乐（retrieval + 游客试听） ----------------
QQ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
QQ_HEAD = {"User-Agent": QQ_UA, "Referer": "https://y.qq.com/"}
QQ_CACHE = {"kw": "", "t": 0, "rows": []}
QQ_URLS = {}


def http_text(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def qq_search(kw, n=30):
    """QQ 音乐公开检索接口（网页版自己用的那个），只要元数据"""
    kw = (kw or "").strip()
    if not kw:
        return []
    if QQ_CACHE["kw"] == kw and time.time() - QQ_CACHE["t"] < 180:
        return QQ_CACHE["rows"]
    url = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&n=%d&p=1&w=%s"
           % (n, urllib.parse.quote(kw)))
    try:
        j = json.loads(http_text(url, QQ_HEAD))
    except Exception as e:
        log_api("qq search fail %r" % e)
        return []
    lst = ((j.get("data") or {}).get("song") or {}).get("list") or []
    rows = []
    for s in lst:
        am = s.get("albummid") or ""
        singer = " / ".join([(x.get("name") or "") for x in (s.get("singer") or [])[:2]])
        rows.append({
            "id": s.get("songmid"),
            "mm": s.get("media_mid") or s.get("songmid"),
            "name": s.get("songname") or "",
            "artist": singer,
            "album": s.get("albumname") or "",
            "dur": int(s.get("interval") or 0),
            "cover": ("https://y.qq.com/music/photo_new/T002R300x300M000%s.jpg" % am) if am else "",
            "pay": 1 if ((s.get("pay") or {}).get("payplay")) else 0,
        })
    QQ_CACHE.update({"kw": kw, "t": time.time(), "rows": rows})
    log_api("qq search %s -> %d" % (kw, len(rows)))
    return rows


def qq_urls(mids):
    """批量问平台要游客试听地址；付费曲平台本身不给，就留空走跨平台映射"""
    mids = [m for m in mids if m]
    if not mids:
        return {}
    body = {"comm": {"uin": "0", "format": "json", "ct": 24, "cv": 0},
            "req": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                    "param": {"guid": "10000", "songmid": mids,
                              "songtype": [0] * len(mids), "uin": "0",
                              "loginflag": 1, "platform": "20"}}}
    try:
        req = urllib.request.Request("https://u.y.qq.com/cgi-bin/musicu.fcg",
                                     data=json.dumps(body).encode(),
                                     headers={"User-Agent": QQ_UA,
                                              "Referer": "https://y.qq.com/",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=14) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        log_api("qq vkey fail %r" % e)
        return {}
    d = ((j.get("req") or {}).get("data")) or {}
    sip = (d.get("sip") or [""])[0]
    out = {}
    for x in (d.get("midurlinfo") or []):
        purl = x.get("purl") or ""
        if purl:
            out[x.get("songmid")] = sip + purl
    return out


def qq_url(mid, mm=""):
    if not mid:
        return None
    hit = QQ_URLS.get(mid)
    if hit and time.time() - hit[1] < 600:
        return hit[0]
    got = qq_urls([mid]).get(mid)
    if got:
        QQ_URLS[mid] = (got, time.time())
    log_api("qq url %s -> %s" % (mid, bool(got)))
    return got


# ---------------- 酷狗直链 ----------------
def kugou_direct(hash_):
    if not hash_:
        return None
    if hash_ in KG_CACHE:
        return KG_CACHE[hash_]
    key = hashlib.md5((hash_ + "kgcloudv2").encode()).hexdigest()
    url = ("http://trackercdn.kugou.com/i/v2/?key=%s&hash=%s&br=128&appid=1005&pid=2&cmd=25&behavior=play"
           % (key, hash_))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            js = json.loads(r.read().decode("utf-8", "ignore"))
        u = (js.get("url") or [None])[0]
        KG_CACHE[hash_] = {"url": u} if u else {"url": None}
    except Exception as e:
        KG_CACHE[hash_] = {"url": None, "why": repr(e)[:80]}
    log_api("kgurl %s -> %s" % (hash_[:8], bool(KG_CACHE[hash_].get("url"))))
    return KG_CACHE[hash_]


# ---------------- 音源层：能播的自己去取，不能播的如实说明 ----------------
SRC_LIST = [
    ("auto", "自动"), ("netease", "网易云"), ("qq", "QQ音乐"), ("kugou", "酷狗"),
    ("kuwo", "酷我"), ("apple", "苹果音乐"), ("deezer", "Deezer"),
]

# 这些音源没有免鉴权直链：选了也能记住偏好，但会自动回落到能播的音源
# 这些平台本身没有免鉴权直链：选了就用同名可播版本（不破解、不盗链）
SRC_MAP = {
    "qq": "QQ 直连：免费曲直接出声，付费曲自动取同名可播版本",
    "netease": "",
    "deezer": "Deezer 只给 30 秒预览，本机网络下接口时好时坏",
}
SRC_NO_PLAY = {k: "" for k in SRC_MAP}

HTTP_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KW_CACHE = {}
APP_CACHE = {}


def http_get(url, timeout=9):
    req = urllib.request.Request(url, headers=HTTP_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def search_kuwo(kw, limit=30):
    u = ("http://search.kuwo.cn/r.s?all=%s&ft=music&itemset=web_2013&client=kt&pn=0&rn=%d"
         "&rformat=json&encoding=utf8" % (urllib.parse.quote(kw), limit))
    raw = http_get(u).decode("utf-8", "ignore").replace("'", '"')
    d = json.loads(raw)
    out = []
    for s in (d.get("abslist") or []):
        name = html.unescape(re.sub("<[^>]+>", "", str(s.get("SONGNAME") or ""))).replace("&nbsp;", " ").strip()
        rid = str(s.get("MUSICRID") or "").replace("MUSIC_", "")
        if not name or not rid:
            continue
        try:
            dur = int(float(s.get("DURATION") or 0))
        except Exception:
            dur = 0
        out.append({"n": name, "a": str(s.get("ARTIST") or "").replace("&nbsp;", " ").strip(),
                    "d": dur, "cover": "", "rid": rid, "collect": None, "src": "酷我"})
    return out


def kuwo_url(rid):
    if not rid:
        return None
    if rid in KW_CACHE:
        return KW_CACHE[rid]
    u = ("http://antiserver.kuwo.cn/anti.s?type=convert_url&format=mp3&response=url&rid=MUSIC_%s"
         % urllib.parse.quote(rid))
    try:
        txt = http_get(u, 8).decode("utf-8", "ignore").strip()
        KW_CACHE[rid] = txt if txt.startswith("http") else None
    except Exception:
        KW_CACHE[rid] = None
    log_api("kwurl %s -> %s" % (rid[:8], bool(KW_CACHE[rid])))
    return KW_CACHE[rid]


def search_apple(kw, limit=30):
    """iTunes 搜索给 30 秒试听直链（苹果音乐没有免鉴权全曲）"""
    cache_key = "it:" + kw
    if cache_key in APP_CACHE:
        return APP_CACHE[cache_key]
    rows = []
    for cc in ("us", "jp", "cn"):
        try:
            u = ("https://itunes.apple.com/search?term=%s&media=music&entity=song&limit=%d&country=%s"
                 % (urllib.parse.quote(kw), limit, cc))
            js = json.loads(http_get(u).decode("utf-8", "ignore"))
        except Exception:
            continue
        for r in (js.get("results") or []):
            pu = r.get("previewUrl") or ""
            if not pu:
                continue
            rows.append({
                "n": str(r.get("trackName") or "").strip(),
                "a": str(r.get("artistName") or "").strip(),
                "d": int((r.get("trackTimeMillis") or 0) // 1000),
                "cover": str(r.get("artworkUrl100") or "").replace("100x100", "200x200"),
                "url": pu, "collect": None, "src": "苹果音乐",
            })
        if len(rows) >= limit:
            break
    APP_CACHE[cache_key] = rows[:limit]
    return APP_CACHE[cache_key]


def src_search(src, kw):
    if not kw:
        return {"rows": [], "why": "empty"}
    if src in ("auto", "kuwo"):
        rows = []
        try:
            rows = search_kuwo(kw)
        except Exception as e:
            return {"rows": [], "why": "酷我搜索失败: %s" % repr(e)[:60]}
        if src == "auto":
            try:
                rows = rows + search_apple(kw)
            except Exception:
                pass
        return {"rows": rows[:60], "ok": True}
    if src == "apple":
        try:
            return {"rows": search_apple(kw), "ok": True}
        except Exception as e:
            return {"rows": [], "why": "苹果音乐搜索失败: %s" % repr(e)[:60]}
    if src in SRC_MAP:
        rows = []
        for fn in (lambda: kugou_rows(kw, 30), lambda: search_kuwo(kw, 30), lambda: search_apple(kw)):
            try:
                got = fn() or []
            except Exception:
                got = []
            for r in got:
                rows.append(r)
        seen, keep = set(), []
        for r in rows:
            k = _nk(r.get("n")) + "|" + _nk(r.get("a"))
            if k in seen:
                continue
            seen.add(k)
            keep.append(r)
        if not keep:
            return {"rows": [], "ok": False, "why": "这个音源连同名版本都没找到"}
        note = SRC_MAP.get(src) or ""
        return {"rows": keep[:60], "ok": True, "mapped": note or "同名可播版本"}
    return {"rows": [], "ok": False, "unsupported": "未知音源"}


def src_catalog():
    return [{"id": i, "label": lb, "playable": True, "why": SRC_MAP.get(i, "")}
            for i, lb in SRC_LIST]



# ---------------- 付费曲兜底：去别家找同名能播的 ----------------
FG_CACHE = {}
FG_ORDER = {"酷狗": 0, "酷我": 1, "咪咕": 2}


def _nk(s):
    return re.sub(r"[（(\[【][^)）\]】]*[)）\]】]|\s|·|．|\.|-|—_~!！?？,，。:：'\"“”‘’/\\|&\u3000", "",
                  str(s or "").lower())


def _peek_ok(url, timeout=7):
    try:
        req = urllib.request.Request(url, headers=dict(HTTP_UA, Range="bytes=0-1023"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(1600)
        return len(b) > 256
    except Exception:
        return False


def kugou_rows(kw, limit=30):
    u = ("http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword=%s&page=1&pagesize=%d&showtype=1"
         % (urllib.parse.quote(kw), limit))
    js = json.loads(http_get(u).decode("utf-8", "ignore"))
    out = []
    for s in ((js.get("data") or {}).get("info") or []):
        nm = (s.get("songname") or "").strip()
        h = s.get("hash")
        if not nm or not h:
            continue
        out.append({"n": nm, "a": (s.get("singername") or "").strip(),
                    "d": int(s.get("duration") or 0), "cover": "", "hash": h,
                    "collect": None, "src": "酷狗"})
    return out


def _kugou_pick(kw):
    u = ("http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword=%s&page=1&pagesize=20&showtype=1"
         % urllib.parse.quote(kw))
    js = json.loads(http_get(u).decode("utf-8", "ignore"))
    for s in ((js.get("data") or {}).get("info") or []):
        nm = (s.get("songname") or "").strip()
        h = s.get("hash")
        if not h or not nm or _nk(nm) != _nk(kw):
            continue
        got = kugou_direct(h)
        if got and got.get("url"):
            return {"url": got["url"], "src": "酷狗", "name": nm}
    return None


def _kuwo_pick(kw):
    for r in search_kuwo(kw, 20):
        if _nk(r["n"]) != _nk(kw):
            continue
        u = kuwo_url(r["rid"])
        if u:
            return {"url": u, "src": "酷我", "name": r["n"]}
    return None


def _migu_pick(kw):
    u = ("https://m.music.migu.cn/migu/remoting/scr_search_tag?keyword=%s&type=2&rows=20&pgc=1"
         % urllib.parse.quote(kw))
    js = json.loads(http_get(u).decode("utf-8", "ignore"))
    for m in (js.get("musics") or []):
        nm = m.get("songName") or ""
        mp3 = m.get("mp3") or m.get("listenUrl") or ""
        if mp3.startswith("http") and _nk(nm) == _nk(kw):
            return {"url": mp3, "src": "咪咕", "name": nm}
    return None


def free_fallback(q, limit=3):
    """并发找同名可播直链；返回的都是各自 peek 过 200 的地址"""
    q = (q or "").strip()
    if not q:
        return []
    key = "fb:" + q
    hit = FG_CACHE.get(key)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    got = []
    lock = threading.Lock()

    def run(fn):
        try:
            r = fn(q)
        except Exception:
            r = None
        if r and r.get("url") and _peek_ok(r["url"]):
            with lock:
                got.append(r)

    ts = [threading.Thread(target=run, args=(f,)) for f in (_kugou_pick, _kuwo_pick, _migu_pick)]
    for th in ts:
        th.daemon = True
        th.start()
    for th in ts:
        th.join(9)
    got.sort(key=lambda x: FG_ORDER.get(x["src"], 9))
    got = got[:limit]
    FG_CACHE[key] = (got, time.time())
    log_api("fallback %s -> %s" % (q, ",".join([g["src"] for g in got]) or "none"))
    return got

# ---------------- 歌词（酷狗 krcs，页面跨域拿不到，交给本地服务） ----------------
LYR_CACHE = {}


def kugou_lyric(hash_, kw, dur):
    key = hash_ or (kw + "|" + str(dur))
    if key in LYR_CACHE:
        return LYR_CACHE[key]
    out = {"lrc": "", "from": "kugou"}
    try:
        import base64
        q = ("http://krcs.kugou.com/search?ver=1&man=yes&client=mobi&keyword=%s&duration=%s&hash=%s"
             % (urllib.parse.quote(kw or ""), int(dur or 0), urllib.parse.quote(hash_ or "")))
        with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": "Mozilla/5.0"}), timeout=9) as r:
            js = json.loads(r.read().decode("utf-8", "ignore"))
        cands = js.get("candidates") or []
        if cands:
            # 同名版本时长差很多（DJ 版 / 童声版）时，按请求时长挑最接近的那条
            c = cands[0]
            if dur:
                c = min(cands, key=lambda k: abs(float(k.get("duration") or 0) - float(dur)))
            out["dur"] = int(float(c.get("duration") or 0))
            out["song"] = c.get("song") or ""
            dl = ("http://lyrics.kugou.com/download?ver=1&client=pc&id=%s&accesskey=%s&fmt=lrc&charset=utf8"
                  % (c.get("id"), urllib.parse.quote(c.get("accesskey") or "")))
            with urllib.request.urlopen(urllib.request.Request(dl, headers={"User-Agent": "Mozilla/5.0"}), timeout=9) as r:
                j2 = json.loads(r.read().decode("utf-8", "ignore"))
            raw = base64.b64decode(j2.get("content") or "")
            out["lrc"] = raw.decode("utf-8", "ignore")
    except Exception as e:
        out["why"] = repr(e)[:100]
    LYR_CACHE[key] = out
    log_api("lyric %s -> %d bytes" % ((hash_ or kw or "")[:10], len(out["lrc"])))
    return out



# ---------------- 优化（清理 / 优先级 / 核分配 / 后台治理 / 网速） ----------------
import ctypes
from ctypes import wintypes

OPT = {"mode": "normal", "bg": 0, "prio": "normal", "cores": "all"}
OPT_LOG = []
BG_TOUCHED = {}          # pid -> 原优先级类
_PRIO_NAME = {"idle": 0x40, "below": 0x4000, "normal": 0x20, "above": 0x8000,
              "high": 0x80, "realtime": 0x100}
_PRIO_READ = {0x40: "idle", 0x4000: "below", 0x20: "normal", 0x8000: "above",
              0x80: "high", 0x100: "realtime"}
_GUARD = {"system", "system idle process", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
          "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "audiodg.exe",
          "fontdrvhost.exe", "ctfmon.exe", "taskhostw.exe", "sihost.exe", "python.exe",
          "pythonw.exe", "Bonfire.exe", "篝火.exe", "胶卷音乐小窗.exe"}


def opt_log(msg):
    OPT_LOG.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
    if len(OPT_LOG) > 60:
        del OPT_LOG[:-60]


def _k32():
    return ctypes.windll.kernel32


def _pid_name(pid):
    k = _k32()
    h = k.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        k.CloseHandle(h)


def _all_pids():
    k = _k32()
    arr = (wintypes.DWORD * 4096)()
    need = wintypes.DWORD()
    if not k.K32EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(need)):
        return []
    n = need.value // ctypes.sizeof(wintypes.DWORD)
    return [arr[i] for i in range(n)]


def _proc_prio(pid):
    k = _k32()
    h = k.OpenProcess(0x0400, False, int(pid))     # QUERY_INFORMATION
    if not h:
        return None
    try:
        return k.GetPriorityClass(h)
    finally:
        k.CloseHandle(h)


def _set_proc_prio(pid, cls):
    k = _k32()
    h = k.OpenProcess(0x0200 | 0x0400, False, int(pid))   # SET_INFORMATION | QUERY_INFORMATION
    if not h:
        return False
    try:
        return bool(k.SetPriorityClass(h, cls))
    finally:
        k.CloseHandle(h)


def opt_prio(level):
    if level not in _PRIO_NAME:
        return {"ok": False, "why": "级别不对"}
    ok = _set_proc_prio(os.getpid(), _PRIO_NAME[level])
    if ok:
        OPT["prio"] = level
        opt_log("本进程优先级 -> %s" % level)
    return {"ok": ok, "prio": OPT["prio"]}


def _cpu_mhz():
    """从注册表读每个逻辑核的标称主频，用来认性能核"""
    out = []
    try:
        import winreg
        path = r"HARDWARE\DESCRIPTION\System\CentralProcessor"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as root:
            i = 0
            while True:
                try:
                    with winreg.OpenKey(root, str(i)) as kk:
                        v, _ = winreg.QueryValueEx(kk, "~MHz")
                        out.append(int(v))
                except OSError:
                    break
                i += 1
    except Exception:
        pass
    return out


def opt_cores(mode):
    k = ctypes.WinDLL("kernel32")
    h = ctypes.c_void_p(k.GetCurrentProcess())
    proc_mask = ctypes.c_ulonglong()
    sys_mask = ctypes.c_ulonglong()
    if not k.GetProcessAffinityMask(h, ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
        return {"ok": False, "why": "拿不到 CPU 掩码"}
    total = bin(sys_mask.value).count("1")
    mhz = _cpu_mhz()
    mask = 0
    if mode == "all":
        mask = sys_mask.value
    elif mode == "pcore" and len(mhz) >= total:
        best = max(mhz[:total])
        for i in range(total):
            if mhz[i] >= best * 0.92:
                mask |= (1 << i)
    elif mode == "noht":
        n = 0
        while n < total:
            mask |= (1 << n)
            n += 2
    else:
        mask = sys_mask.value
    if mask == 0:
        mask = sys_mask.value
    ok = bool(k.SetProcessAffinityMask(h, ctypes.c_ulonglong(mask)))
    if ok:
        OPT["cores"] = mode
        opt_log("核心分配 -> %s（%d/%d 核）" % (mode, bin(mask).count("1"), total))
    return {"ok": ok, "cores": OPT["cores"], "use": bin(mask).count("1"), "total": total}


def opt_bg(level):
    level = int(level)
    if level == 0:
        back = 0
        for pid, old in list(BG_TOUCHED.items()):
            if old and _set_proc_prio(pid, old):
                back += 1
        BG_TOUCHED.clear()
        OPT["bg"] = 0
        opt_log("后台治理关闭，恢复 %d 个进程" % back)
        return {"ok": True, "bg": 0, "restored": back}
    cls = _PRIO_NAME["below"] if level == 1 else _PRIO_NAME["idle"]
    want = "正常" if level == 1 else "激进"
    self_pid = os.getpid()
    touched, skip = 0, 0
    for pid in _all_pids():
        if not pid or pid == self_pid:
            continue
        nm = _pid_name(pid).lower()
        if not nm or nm in _GUARD:
            skip += 1
            continue
        cur = _proc_prio(pid)
        if cur is None or cur == cls:
            continue
        if _set_proc_prio(pid, cls):
            BG_TOUCHED.setdefault(pid, cur)
            touched += 1
    OPT["bg"] = level
    opt_log("后台治理 -> %s（降级 %d 个进程，跳过 %d）" % (want, touched, skip))
    return {"ok": True, "bg": level, "touched": touched, "skip": skip}


def _walk_bytes(root):
    total = 0
    for dirpath, dirnames, files in os.walk(root, topdown=True):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except Exception:
                pass
    return total


CACHE_KEEP = {"port.txt", "state.json", "bg.bin", "bg.json"}


def _purge_cache():
    """只删缓存（api.log 等），保住 port.txt / state.json / 用户上传的背景"""
    freed = 0
    try:
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            if name in CACHE_KEEP or name.startswith("bg"):
                continue
            if os.path.isdir(p):
                freed += _walk_purge(p)
                try:
                    os.rmdir(p)
                except Exception:
                    pass
            else:
                try:
                    sz = os.path.getsize(p)
                    os.remove(p)
                    freed += sz
                except Exception:
                    pass
    except Exception:
        pass
    return freed


def _walk_purge(root):
    freed = 0
    for dirpath, dirnames, files in os.walk(root, topdown=True):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                freed += sz
            except Exception:
                pass
    return freed


def opt_targets():
    t = []
    t.append(("temp", "用户临时文件", os.environ.get("TEMP") or r"C:\Windows\Temp"))
    t.append(("win_temp", "系统临时文件", r"C:\Windows\Temp"))
    t.append(("appcache", "播放器缓存", CACHE_DIR))
    t.append(("logs", "接口日志", API_LOG))
    return t


OPT_SCAN = {"t": 0.0, "data": None}
OPT_LAT = {"t": 0.0, "data": None}
OPT_EXT = {"timer": 0, "game": 0, "iso": 0, "dwm": 0, "power": "unknown", "ifeo": {}}
ISO_TOUCHED = {}
DWM_OLD = {}


def opt_scan(force=False):
    if not force and OPT_SCAN["data"] and time.time() - OPT_SCAN["t"] < 8:
        return OPT_SCAN["data"]
    items, total = [], 0
    for key, label, path in opt_targets():
        try:
            if os.path.isfile(path):
                sz = os.path.getsize(path)
            elif key == "appcache":
                sz = _walk_bytes(path)
                try:
                    sz -= os.path.getsize(API_LOG)
                except Exception:
                    pass
            else:
                sz = _walk_bytes(path)
        except Exception:
            sz = 0
        items.append({"key": key, "label": label, "size": sz, "path": str(path)})
        total += sz
    try:
        free = 0
        drive = os.environ.get("SystemDrive") or "C:"
        free = shutil.disk_usage(drive + os.sep).free
    except Exception:
        free = 0
    res = {"items": items, "total": total, "free": free}
    OPT_SCAN["data"] = res
    OPT_SCAN["t"] = time.time()
    return res


def opt_clean(keys):
    freed, done = 0, []
    for key, label, path in opt_targets():
        if key not in keys:
            continue
        try:
            if key == "appcache":
                freed += _purge_cache()
            elif key == "logs":
                try:
                    sz = os.path.getsize(API_LOG)
                    os.remove(API_LOG)
                    freed += sz
                except Exception:
                    pass
            elif os.path.isfile(path):
                sz = os.path.getsize(path)
                os.remove(path)
                freed += sz
            else:
                freed += _walk_purge(path)
            done.append(label)
        except Exception:
            pass
    if "recycle" in keys:
        try:
            SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 1, 2, 4
            ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
            done.append("回收站")
        except Exception:
            pass
    _run([_sys32("ipconfig.exe"), "/flushdns"])
    opt_log("极限清理：%s，释放 %.1f MB" % ("、".join(done), freed / 1048576.0))
    return {"ok": True, "freed": freed, "done": done}


def opt_latency(hosts=None, force=False):
    import socket
    if not force and not hosts and OPT_LAT["data"] and time.time() - OPT_LAT["t"] < 120:
        return OPT_LAT["data"]
    hosts = hosts or [("music.163.com", 443), ("mobilecdn.kugou.com", 80),
                      ("search.kuwo.cn", 80), ("c.y.qq.com", 443)]
    out = []
    for host, port in hosts:
        best = None
        for _ in range(3):
            try:
                t0 = time.time()
                s = socket.create_connection((host, port), 2.0)
                s.close()
                ms = (time.time() - t0) * 1000
                best = ms if best is None else min(best, ms)
            except Exception:
                pass
        out.append({"host": host, "ms": round(best, 1) if best else None})
    if not hosts:
        OPT_LAT["data"] = out
        OPT_LAT["t"] = time.time()
    return out




CREATE_NO_WINDOW = 0x08000000


def _run(args, timeout=12):
    """跑系统命令但绝不弹黑框：无窗口 + 通过 stdin/stdout 全接管"""
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, startupinfo=si,
                           stdin=subprocess.DEVNULL)
        out = b""
        try:
            out = (p.stdout or b"") + (p.stderr or b"")
        except Exception:
            out = b""
        try:
            txt = out.decode("utf-8")
        except Exception:
            try:
                txt = out.decode("mbcs")
            except Exception:
                txt = out.decode("latin-1", "ignore")
        return p.returncode, txt
    except Exception as e:
        return -1, str(e)


def _sys32(name):
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", name)


def opt_timer(on):
    on = int(on)
    try:
        if on:
            ctypes.windll.winmm.timeBeginPeriod(1)
            OPT_EXT["timer"] = 1
            opt_log("计时器精度 -> 1ms（timeBeginPeriod）")
        else:
            ctypes.windll.winmm.timeEndPeriod(1)
            OPT_EXT["timer"] = 0
            opt_log("计时器精度 -> 恢复系统默认")
        return {"ok": True, "timer": OPT_EXT["timer"]}
    except Exception as e:
        return {"ok": False, "why": str(e)}


def _enable_priv(name):
    try:
        adv = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        tok = ctypes.c_void_p()
        if not adv.OpenProcessToken(ctypes.c_void_p(k32.GetCurrentProcess()), 0x28, ctypes.byref(tok)):
            return False
        luid = ctypes.c_ulonglong()
        if not adv.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            k32.CloseHandle(tok)
            return False

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", ctypes.c_ulonglong), ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x00000002
        ok = adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
        k32.CloseHandle(tok)
        return bool(ok)
    except Exception:
        return False


def opt_mem():
    """清工作集 + 尝试清 standby list（Pavise 的内存清理）"""
    freed_ws = 0
    psapi = ctypes.windll.psapi
    for pid in _all_pids():
        if not pid:
            continue
        try:
            k = _k32()
            h = k.OpenProcess(0x0100 | 0x0400, False, int(pid))   # SET_QUOTA | QUERY_INFORMATION
            if not h:
                continue
            if psapi.EmptyWorkingSet(h):
                freed_ws += 1
            k.CloseHandle(h)
        except Exception:
            pass
    purge = False
    try:
        _enable_priv("SeProfileSingleProcessPrivilege")
        nt = ctypes.WinDLL("ntdll")
        cmd = ctypes.c_int(4)     # MemoryPurgeStandbyList
        nt.NtSetSystemInformation(80, ctypes.byref(cmd), ctypes.sizeof(cmd))
        purge = True
    except Exception:
        purge = False
    opt_log("内存清理：%d 个进程工作集被回收，待机列表%s" % (freed_ws, "已清" if purge else "需要管理员权限"))
    return {"ok": True, "procs": freed_ws, "standby": purge}


def winreg_open_hkcu():
    import winreg
    return winreg.HKEY_CURRENT_USER


def _reg_set(root, path, name, val):
    import winreg
    with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(val))


def opt_game(on):
    on = int(on)
    try:
        _reg_set(winreg_open_hkcu(), r"SOFTWARE\Microsoft\GameBar", "AutoGameModeEnabled", on)
        OPT_EXT["game"] = on
        opt_log("Windows 游戏模式 -> %s" % ("开" if on else "关"))
        return {"ok": True, "game": on}
    except Exception as e:
        return {"ok": False, "why": str(e)}


def opt_power(plan):
    scheme = "SCHEME_MIN" if plan == "high" else "SCHEME_BALANCED"
    try:
        rc, _ = _run([_sys32("powercfg.exe"), "/setactive", scheme])
        if rc != 0:
            return {"ok": False, "why": "powercfg 失败（rc=%d），可能方案不存在" % rc}
        OPT_EXT["power"] = "high" if plan == "high" else "balanced"
        opt_log("电源计划 -> %s" % ("高性能" if plan == "high" else "平衡"))
        return {"ok": True, "power": OPT_EXT["power"]}
    except Exception as e:
        return {"ok": False, "why": str(e)}


def opt_dwm(on):
    on = int(on)
    try:
        for pid in _all_pids():
            if _pid_name(pid).lower() != "dwm.exe":
                continue
            if on:
                BG_TOUCHED.setdefault(pid, _proc_prio(pid))
                _set_proc_prio(pid, _PRIO_NAME["high"])
            else:
                old = BG_TOUCHED.pop(pid, None)
                if old:
                    _set_proc_prio(pid, old)
        OPT_EXT["dwm"] = on
        opt_log("桌面窗口管理器 -> %s" % ("高优先级" if on else "恢复"))
        return {"ok": True, "dwm": on}
    except Exception as e:
        return {"ok": False, "why": str(e)}


def opt_iso(on):
    """把后台进程赶到保留核，给前台留出干净核（Pavise 的核心隔离）"""
    on = int(on)
    k = ctypes.WinDLL("kernel32")
    self_pid = os.getpid()

    def _open(pid, right):
        return ctypes.c_void_p(k.OpenProcess(right, False, int(pid)))

    if not on:
        back = 0
        for pid, (mask, old) in list(ISO_TOUCHED.items()):
            try:
                h = _open(pid, 0x0200)
                if h:
                    if k.SetProcessAffinityMask(h, ctypes.c_ulonglong(old or mask)):
                        back += 1
                    k.CloseHandle(h)
            except Exception:
                pass
        ISO_TOUCHED.clear()
        OPT_EXT["iso"] = 0
        opt_log("核心隔离关闭，恢复 %d 个进程" % back)
        return {"ok": True, "iso": 0, "restored": back}

    h = _open(self_pid, 0x0400)
    pm, sm = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not h or not k.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm)):
        return {"ok": False, "why": "拿不到 CPU 掩码"}
    k.CloseHandle(h)
    total = bin(sm.value).count("1")
    start = max(1, total - max(1, total // 4))
    mask = 0
    for i in range(start, total):
        mask |= (1 << i)
    if mask == 0:
        return {"ok": False, "why": "核数不够，隔离不了"}
    touched = 0
    for pid in _all_pids():
        if not pid or pid == self_pid:
            continue
        nm = _pid_name(pid).lower()
        if not nm or nm in _GUARD:
            continue
        try:
            hp = _open(pid, 0x0400 | 0x0200)
            if not hp:
                continue
            a, b = ctypes.c_ulonglong(), ctypes.c_ulonglong()
            if not k.GetProcessAffinityMask(hp, ctypes.byref(a), ctypes.byref(b)):
                k.CloseHandle(hp)
                continue
            if k.SetProcessAffinityMask(hp, ctypes.c_ulonglong(mask)):
                ISO_TOUCHED[pid] = (mask, a.value)
                touched += 1
            k.CloseHandle(hp)
        except Exception:
            pass
    OPT_EXT["iso"] = 1
    opt_log("核心隔离：后台 %d 个进程限定到后 %d 核" % (touched, total - start))
    return {"ok": True, "iso": 1, "touched": touched, "cores": total - start}


def opt_dwm(on):
    on = int(on)
    try:
        for pid in _all_pids():
            if _pid_name(pid).lower() != "dwm.exe":
                continue
            if on:
                DWM_OLD.setdefault(pid, _proc_prio(pid))
                _set_proc_prio(pid, _PRIO_NAME["high"])
            else:
                old = DWM_OLD.pop(pid, None)
                if old:
                    _set_proc_prio(pid, old)
        OPT_EXT["dwm"] = on
        opt_log("桌面窗口管理器 -> %s" % ("高优先级" if on else "恢复"))
        return {"ok": True, "dwm": on}
    except Exception as e:
        return {"ok": False, "why": str(e)}


def opt_sys_probe():
    """读真实系统状态，避免界面状态和系统不一致"""
    import winreg
    if OPT_EXT.get("power") == "unknown":
        try:
            _, txt = _run([_sys32("powercfg.exe"), "/getactivescheme"], 8)
            if "381b4222-f694-41f0-9685-ff5bb260df2e" in txt:
                OPT_EXT["power"] = "balanced"
            elif "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c" in txt:
                OPT_EXT["power"] = "high"
            else:
                opt_log("电源计划探测未命中：%s" % txt.strip()[:80])
        except Exception as e:
            OPT_EXT["power"] = "unknown"
            opt_log("电源计划探测失败：%r" % e)
    if "game" not in OPT_EXT or OPT_EXT.get("game_src") != "reg":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\\Microsoft\\GameBar") as k:
                OPT_EXT["game"] = int(winreg.QueryValueEx(k, "AutoGameModeEnabled")[0])
                OPT_EXT["game_src"] = "reg"
        except Exception:
            pass
    return OPT_EXT



# ---------------- 设备中断策略：MSI / IRQ 优先级 / 中断亲和 ----------------
DEV_CACHE = []
DEV_BACKUP = {}          # regpath|value -> 原值（None 表示原本不存在）
ACC_GUARD = ["vgc.exe", "vgtray.exe", "easyanticheat.exe", "beservice.exe",
             "ace-guard client.exe", "gameguard.des", "tenprotect", "mhyprot2.exe"]
ACC_ON = {"on": 0}


def opt_is_admin():
    try:
        adv = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        tok = ctypes.c_void_p()
        if not adv.OpenProcessToken(ctypes.c_void_p(k32.GetCurrentProcess()), 0x0008, ctypes.byref(tok)):
            return False
        elev = ctypes.c_ulong()
        size = ctypes.c_ulong()
        ok = adv.GetTokenInformation(tok, 20, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(size))
        k32.CloseHandle(tok)
        return bool(ok) and bool(elev.value)
    except Exception:
        return False


def opt_elevate():
    """以管理员权限重启自己（UAC）"""
    try:
        exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
        if not getattr(sys, "frozen", False):
            return {"ok": False, "why": "开发模式用 PowerShell 以管理员启动即可"}
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, os.getcwd(), 1)
        if rc and rc > 32:
            threading.Timer(1.2, lambda: os._exit(0)).start()
            return {"ok": True, "why": "已申请管理员权限，确认 UAC 后新窗口会接管"}
        return {"ok": False, "why": "UAC 被拒绝或取消（rc=%s）" % rc}
    except Exception as e:
        return {"ok": False, "why": str(e)}


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Bonfire"


def autostart_cmd():
    """注册表里写的启动命令：打包后直接指向 exe，开发模式指向 pythonw + app.py"""
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def autostart_state():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val = winreg.QueryValueEx(k, RUN_NAME)[0]
        return {"on": True, "cmd": val}
    except Exception:
        return {"on": False, "cmd": ""}


def autostart_set(on):
    import winreg
    ok = False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        ok = True
    except Exception as e:
        return {"ok": False, "on": False, "why": str(e)}
    # 记一份到 state.json：下次启动发现注册表被清掉（优化软件、系统清理）能自己补回来
    save_state({"auto_boot": bool(on)})
    return {"ok": ok, "on": bool(on)}


def _reg_read(path, name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def _reg_write(path, name, kind, val):
    import winreg
    key = (path + "|" + name).lower()
    if key not in DEV_BACKUP:
        DEV_BACKUP[key] = _reg_read(path, name)
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, kind, val)


def _dev_kind(name, service):
    low = (name or "").lower() + " " + (service or "").lower()
    if any(w in low for w in ("ethernet", "以太网", "wifi", "wi-fi", "wireless", "无线", "realtek pcie", "netcard")):
        return "net"
    if any(w in low for w in ("nvidia", "geforce", "radeon", "amd", "intel uhd", "graphics", "显卡", "显示")):
        return "gpu"
    if any(w in low for w in ("keyboard", "键盘", "mouse", "鼠标", "hid")):
        return "hid"
    return ""


def opt_devices(force=False):
    """枚举网卡 / 显卡 / 键鼠的中断配置"""
    global DEV_CACHE
    if DEV_CACHE and not force:
        return DEV_CACHE
    import winreg
    root = r"SYSTEM\CurrentControlSet\Enum\PCI"
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as rk:
            i = 0
            while True:
                try:
                    dev = winreg.EnumKey(rk, i)
                except OSError:
                    break
                i += 1
                if not dev.startswith("VEN_"):
                    continue
                try:
                    with winreg.OpenKey(rk, dev) as dk:
                        j = 0
                        while True:
                            try:
                                inst = winreg.EnumKey(dk, j)
                            except OSError:
                                break
                            j += 1
                            ipath = root + "\\" + dev + "\\" + inst
                            name = _reg_read(ipath, "FriendlyName") or _reg_read(ipath, "DeviceDesc") or dev
                            service = _reg_read(ipath, "Service") or ""
                            kind = _dev_kind(str(name), str(service))
                            if not kind:
                                continue
                            mp = ipath + r"\Device Parameters\Interrupt Management" \
                                        r"\MessageSignaledInterruptProperties"
                            ap = ipath + r"\Device Parameters\Interrupt Management\Affinity Policy"
                            out.append({
                                "id": len(out), "name": str(name).split(";")[-1].strip(),
                                "kind": kind, "path": ipath,
                                "msi": _reg_read(mp, "MSISupported"),
                                "prio": _reg_read(ap, "DevicePriority"),
                                "aff": _reg_read(ap, "DevicePolicy"),
                            })
                except Exception:
                    continue
    except Exception:
        pass
    DEV_CACHE = out
    return out


def opt_dev_msi(idx, on):
    import winreg
    d = opt_devices()
    if idx < 0 or idx >= len(d):
        return {"ok": False, "why": "设备编号越界"}
    path = d[idx]["path"] + r"\Device Parameters\Interrupt Management" \
                            r"\MessageSignaledInterruptProperties"
    try:
        _reg_write(path, "MSISupported", winreg.REG_DWORD, int(on))
        d[idx]["msi"] = int(on)
        opt_log("MSI 模式：%s -> %s（重启后生效）" % (d[idx]["name"], "开" if on else "关"))
        return {"ok": True, "msi": int(on), "need_admin": not opt_is_admin()}
    except Exception as e:
        return {"ok": False, "why": _err_admin(e)}


def opt_dev_prio(idx, v):
    import winreg
    d = opt_devices()
    if idx < 0 or idx >= len(d):
        return {"ok": False, "why": "设备编号越界"}
    path = d[idx]["path"] + r"\Device Parameters\Interrupt Management\Affinity Policy"
    try:
        _reg_write(path, "DevicePriority", winreg.REG_DWORD, int(v))
        d[idx]["prio"] = int(v)
        opt_log("IRQ 优先级：%s -> %s" % (d[idx]["name"], v))
        return {"ok": True, "prio": int(v), "need_admin": not opt_is_admin()}
    except Exception as e:
        return {"ok": False, "why": _err_admin(e)}


def opt_dev_aff(idx, mode):
    """把设备中断绑到指定核：mode=high(前1/4) / low(后1/4) / all"""
    import winreg
    d = opt_devices()
    if idx < 0 or idx >= len(d):
        return {"ok": False, "why": "设备编号越界"}
    k = ctypes.WinDLL("kernel32")
    h = ctypes.c_void_p(k.GetCurrentProcess())
    pm, sm = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not k.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm)):
        return {"ok": False, "why": "拿不到 CPU 掩码"}
    total = bin(sm.value).count("1")
    quarter = max(1, total // 4)
    if mode == "high":
        mask = (1 << quarter) - 1
        policy = 4
    elif mode == "low":
        mask = 0
        for i in range(total - quarter, total):
            mask |= (1 << i)
        policy = 4
    else:
        mask = 0
        policy = 2
    path = d[idx]["path"] + r"\Device Parameters\Interrupt Management\Affinity Policy"
    try:
        _reg_write(path, "DevicePolicy", winreg.REG_DWORD, policy)
        if policy == 4:
            raw = mask.to_bytes(8, "little")
            _reg_write(path, "AssignmentSetOverride", winreg.REG_BINARY, raw)
        d[idx]["aff"] = policy
        opt_log("中断亲和：%s -> %s（掩码 0x%x，重启后生效）" % (d[idx]["name"], mode, mask))
        return {"ok": True, "aff": policy, "mask": mask, "need_admin": not opt_is_admin()}
    except Exception as e:
        return {"ok": False, "why": _err_admin(e)}


def _err_admin(e):
    return "需要管理员权限才能改设备中断策略" if "5" in str(e) else str(e)


def opt_dev_restore():
    import winreg
    n = 0
    for key, old in list(DEV_BACKUP.items()):
        path, name = key.rsplit("|", 1)
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE) as k:
                if old is None:
                    winreg.DeleteValue(k, name)
                else:
                    kind = winreg.REG_BINARY if isinstance(old, bytes) else winreg.REG_DWORD
                    winreg.SetValueEx(k, name, 0, kind, old)
            n += 1
        except Exception:
            pass
    DEV_BACKUP.clear()
    opt_devices(force=True)
    opt_log("设备中断策略已还原 %d 项" % n)
    return {"ok": True, "restored": n}


def opt_acc(on):
    ACC_ON["on"] = int(on)
    names = set(ACC_GUARD)
    if on:
        _GUARD.update(names)
        opt_log("反作弊兼容：已把 %d 个反作弊进程加入保护" % len(names))
    else:
        _GUARD.difference_update(names)
        opt_log("反作弊兼容关闭")
    return {"ok": True, "acc": ACC_ON["on"]}


def _rtss_dirs():
    cands = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "RivaTuner Statistics Server"))
    cands.append(r"C:\Program Files (x86)\RivaTuner Statistics Server")
    return [c for c in cands if os.path.isdir(c)]


def opt_rtss(fps=None):
    """帧率限制：有 RTSS 就写 profile，没有就如实说明依赖"""
    import winreg
    dirs = _rtss_dirs()
    entry = None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\RTSS") as k:
            entry = winreg.QueryValueEx(k, "InstallLocation")[0]
    except Exception:
        entry = None
    if entry and os.path.isdir(entry) and entry not in dirs:
        dirs.append(entry)
    if not dirs:
        return {"ok": False, "installed": False,
                "why": "本机没装 RTSS。Pavise 的帧率限制也是靠 RTSS 驱动实现的，装了 RTSS 后这里会自动接管"}
    if fps is None:
        cur = None
        prof = os.path.join(dirs[0], "Profiles", "Global")
        try:
            with open(prof, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("Limit="):
                        cur = line.strip().split("=", 1)[1]
        except Exception:
            pass
        return {"ok": True, "installed": True, "fps": cur, "dir": dirs[0]}
    try:
        prof = os.path.join(dirs[0], "Profiles", "Global")
        lines = []
        if os.path.isfile(prof):
            with open(prof, encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        kept = [x for x in lines if not x.startswith("Limit=")]
        kept.append("Limit=%d" % int(fps))
        with open(prof, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        opt_log("帧率限制：全局 %s FPS（RTSS）" % fps)
        return {"ok": True, "installed": True, "fps": int(fps)}
    except Exception as e:
        return {"ok": False, "installed": True, "why": str(e)}


RTSS_PAGE = "https://www.guru3d.com/files-details/rtss-rivatuner-statistics-server-download.html"


def pick_exe_file(title="选择安装包"):
    """单个 exe 选择框（用于挑第三方安装包，不下载、不打包）"""
    try:
        user32 = ctypes.windll.user32
        comdlg32 = ctypes.windll.comdlg32
        OFN_EXPLORER = 0x00080000
        OFN_FILEMUSTEXIST = 0x00001000
        OFN_PATHMUSTEXIST = 0x00000800

        class OPENFILENAME(ctypes.Structure):
            _fields_ = [("lStructSize", ctypes.c_uint), ("hwndOwner", wintypes.HWND),
                        ("hInstance", ctypes.c_void_p), ("lpstrFilter", wintypes.LPCWSTR),
                        ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", ctypes.c_uint),
                        ("nFilterIndex", ctypes.c_uint), ("lpstrFile", wintypes.LPWSTR),
                        ("nMaxFile", ctypes.c_uint), ("lpstrFileTitle", wintypes.LPWSTR),
                        ("nMaxFileTitle", ctypes.c_uint), ("lpstrInitialDir", wintypes.LPCWSTR),
                        ("lpstrTitle", wintypes.LPCWSTR), ("Flags", ctypes.c_uint),
                        ("nFileOffset", ctypes.c_ushort), ("nFileExtension", ctypes.c_ushort),
                        ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                        ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR)]

        buf = ctypes.create_unicode_buffer(8000)
        of = OPENFILENAME()
        of.lStructSize = ctypes.sizeof(OPENFILENAME)
        of.hwndOwner = find_hwnd()
        of.lpstrFilter = "安装程序\0*.exe\0所有文件\0*.*\0\0"
        of.nFilterIndex = 1
        of.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        of.nMaxFile = 8000
        of.lpstrTitle = title
        of.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
        if not comdlg32.GetOpenFileNameW(ctypes.byref(of)):
            return ""
        p = buf.value.strip()
        return p if p and os.path.isfile(p) else ""
    except Exception as e:
        log_api("pick_exe_file EXC %r" % e)
        return ""


def opt_rtss_install(path=None):
    """装了就直接用；没装就引导：选本地安装包→UAC 启动，取消则打开官方下载页"""
    st = opt_rtss(None)
    if st.get("installed"):
        return {"ok": True, "installed": True, "why": "RTSS 已经装好了，直接写限帧就行"}
    exe = path or pick_exe_file("选择 RTSS 官方安装包（rtsssetup.exe）")
    if not exe:
        try:
            os.startfile(RTSS_PAGE)
            return {"ok": True, "installed": False, "opened": True,
                    "why": "已打开 RTSS 官方下载页，下载完再点一次这里选安装包"}
        except Exception as e:
            return {"ok": False, "installed": False, "why": "打不开下载页：%s" % e}
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, os.path.dirname(exe), 1)
        if rc and rc > 32:
            return {"ok": True, "installed": False, "launched": True, "path": exe,
                    "why": "已调用 RTSS 安装程序，装完回来刷新一次"}
        return {"ok": False, "installed": False, "why": "UAC 被拒绝或取消（rc=%s）" % rc}
    except Exception as e:
        return {"ok": False, "installed": False, "why": str(e)}


def _lol_dirs():
    out = []
    for base in ("C:", "D:", "E:", "F:"):
        for rel in (r"\Riot Games\League of Legends", r"\WeGameApps\英雄联盟", r"\腾讯游戏\英雄联盟"):
            p = base + rel
            if os.path.isdir(p):
                out.append(p)
    return out


def opt_gamefiles(do_clean=False):
    """游戏插件清理：只清已知的 LoL 缓存 / 插件目录"""
    targets = []
    for d in _lol_dirs():
        for rel in (r"\Game\DATA\FINAL\Champions", r"\Game\DATA\Shared", r"\Game\Logs",
                    r"\Game\Config\Logs", r"\TCLS\TencentDL"):
            p = d + rel
            if os.path.isdir(p):
                targets.append(p)
    if not do_clean:
        return {"ok": True, "found": [{"path": p, "size": _walk_bytes(p)} for p in targets],
                "installed": bool(_lol_dirs())}
    freed = 0
    done = []
    for p in targets:
        try:
            freed += _walk_purge(p)
            done.append(p)
        except Exception:
            pass
    opt_log("游戏插件清理：%d 个目录，释放 %.1f MB" % (len(done), freed / 1048576.0))
    return {"ok": True, "freed": freed, "done": len(done)}


def opt_ifeo(exe, prio):
    """给指定 exe 写 IFEO 优先级（Pavise 的 per-game 优先级），可还原"""
    import winreg
    exe = (exe or "").strip()
    if not exe:
        return {"ok": False, "why": "没写程序名"}
    if not exe.lower().endswith(".exe"):
        exe += ".exe"
    cls = {"idle": 1, "normal": 2, "high": 3}.get(prio)
    if not cls:
        return {"ok": False, "why": "级别不对"}
    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\%s\PerfOptions" % exe
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            old = None
            try:
                old = winreg.QueryValueEx(k, "CpuPriorityClass")[0]
            except OSError:
                old = None
            if old is not None and exe.lower() not in OPT_EXT["ifeo"]:
                OPT_EXT["ifeo"][exe.lower()] = old
            winreg.SetValueEx(k, "CpuPriorityClass", 0, winreg.REG_DWORD, cls)
        OPT_EXT["ifeo"][exe.lower()] = OPT_EXT["ifeo"].get(exe.lower(), 0)
        opt_log("IFEO 优先级：%s -> %s（需管理员才生效）" % (exe, prio))
        return {"ok": True, "exe": exe, "prio": prio}
    except Exception as e:
        msg = "需要管理员权限才能写 IFEO" if "5" in str(e) else str(e)
        opt_log("IFEO 写入失败：%s" % msg)
        return {"ok": False, "why": msg}


def opt_ifeo_clear():
    import winreg
    n = 0
    for exe in list(OPT_EXT["ifeo"].keys()):
        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\%s\PerfOptions" % exe
        old = OPT_EXT["ifeo"].get(exe)
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_SET_VALUE) as k:
                if old:
                    winreg.SetValueEx(k, "CpuPriorityClass", 0, winreg.REG_DWORD, int(old))
                else:
                    winreg.DeleteValue(k, "CpuPriorityClass")
            n += 1
        except Exception:
            pass
        OPT_EXT["ifeo"].pop(exe, None)
    opt_log("IFEO 优先级已清理 %d 项" % n)
    return {"ok": True, "cleared": n}


def _run_elevated(cmdline, wait=30):
    """UAC 提权执行一条命令（弹一次系统授权框），返回 (ok, exit_code)"""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("fMask", ctypes.c_ulong),
                    ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR),
                    ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD),
                    ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.lpVerb = "runas"
    sei.lpFile = "cmd.exe"
    sei.lpParameters = "/c " + cmdline
    sei.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        opt_log("UAC 被取消或失败")
        return False, -1
    if not sei.hProcess:
        return False, -1
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, int(wait * 1000))
    code = wintypes.DWORD(-1)
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return True, code.value


def _tcp_state():
    """读 netsh 全局 TCP 参数：只认值（normal / enabled），不依赖界面语言"""
    rc, out = _run([_sys32("netsh.exe"), "int", "tcp", "show", "global"])
    txt = out or ""
    st = {"tcp": False, "ecn": False}
    for line in txt.splitlines():
        if ":" not in line:
            continue
        label, val = line.split(":", 1)
        val = val.strip().lower()
        if val == "normal":
            st["tcp"] = True
        if val == "enabled" and "ecn" in label.lower():
            st["ecn"] = True
    return st


def opt_net():
    r = {}
    rc, _o = _run([_sys32("ipconfig.exe"), "/flushdns"])
    r["dns"] = rc == 0
    before = _tcp_state()
    r["tcp"] = before["tcp"]
    r["ecn"] = before["ecn"]
    r["elevated"] = False
    if not (r["tcp"] and r["ecn"]):
        cmds = []
        if not r["tcp"]:
            cmds.append("netsh int tcp set global autotuninglevel=normal")
        if not r["ecn"]:
            cmds.append("netsh int tcp set global ecncapability=enabled")
        opt_log("缺项 %s，走 UAC 提权" % (cmds,))
        ok, code = _run_elevated(" & ".join(cmds))
        r["elevated"] = bool(ok)
        r["elevated_rc"] = code
        after = _tcp_state()
        r["tcp"] = after["tcp"]
        r["ecn"] = after["ecn"]
    r["admin"] = bool(r["tcp"] and r["ecn"])
    r["lat"] = opt_latency(force=True)
    opt_log("网速优化：DNS=%s TCP=%s ECN=%s 提权=%s" % (r["dns"], r["tcp"], r["ecn"], r["elevated"]))
    return r


# ---------------- AI 控制能力清单 ----------------
API_DOC = {
    "name": "Bonfire · 本地控制接口",
    "base": "http://127.0.0.1:<port>（端口见 %LOCALAPPDATA%\\FilmMusicWindow\\port.txt）",
    "endpoints": {
        "GET /push?q=<歌名>": "按歌名搜索并立刻播放（同名版本按收藏量排序）",
        "GET /push?cmd=<指令>": "执行一条指令，见 commands",
        "GET /stat": "读当前状态：曲名/列表数/进度/倍速/音源/语言/视图/播放模式/歌词行数/窗口矩形",
        "GET /lyric?hash=&kw=&dur=": "按酷狗 hash 取 LRC 文本",
        "GET /kgurl?hash=": "取酷狗直链",
        "GET /srcs": "音源清单（18 项，含是否能真播的说明）",
        "GET /srcsearch?src=<音源>&q=<歌名>": "按指定音源搜索，统一返回 rows（酷我 / 苹果试听能播，其余给 unsupported 原因）",
        "GET /kwurl?rid=<酷我 rid>": "取酷我播放直链",
        "GET /ball?on=1|0": "悬浮球模式：把整窗缩成小球 / 还原",
        "GET /toggle_top / /set_top?on=0|1": "置顶开关",
        "GET /move?dx=&dy=": "按像素增量移动窗口",
        "GET /resize?h=": "按内容高度调整窗口",
        "GET /proxy?u=<url>": "本地同源代理：搜索/歌词接口走它，绕开跨域和第三方代理限流",
        "GET /download?url=&name=&ext=": "把直链音频下到 D 盘「音乐下载」，自动避开 HTML 假音频",
        "GET /dlpath?p=<目录>": "查询 / 设置下载目录（空参=只看）",
        "GET /dlpick": "弹出目录选择框，选完即设为下载目录",
        "GET /openfolder?p=": "打开下载目录",
        "POST /bgup（body=文件）": "上传本地视频/音频当背景，缓存到本机",
        "GET /bgmedia": "流播缓存的背景视频/音频，支持 Range",
        "POST /txt?name=<名字>（body=文本）": "歌词导出成 txt，落到下载目录",
        "GET /boost": "优化页状态：模式/后台治理/优先级/核心 + 可清理体积 + 各站延迟",
        "GET /boost/net": "优化网速：刷 DNS、TCP 自动调优 normal、ECN，并回测延迟",
        "GET /boost/clean?items=temp,win_temp,appcache,recycle": "极限清理，回传释放字节与已清项",
        "GET /boost/prio?p=normal|above|high": "本进程 CPU 优先级",
        "GET /boost/cores?m=all|pcore|noht": "核心分配：全核 / 仅性能核 / 禁超线程",
        "GET /boost/bg?l=0|1|2": "后台治理档位，0 按原值回滚",
        "GET /boost/mode?m=normal|comp|custom": "模式档位（竞技会锁定高优先级）",
        "GET /close": "退出程序",
    },
    "commands": [
        "__view_list / __view_main / __back", "__search_list", "__click_gear / __close_set",
        "__next_song / __prev_song / __play_now / __pause_now / __press_space",
        "__press_back（后退5秒）/ __press_hold2x / __press_release2x",
        "__toggle_vol / __vol_up / __vol_down",
        "__click_rate（展开倍速列表）/ __rate_0.75 / __rate_1 / __rate_1.25 / __rate_1.5 / __rate_2",
        "__cycle_mode / __mode_single / __mode_order",
        "__src_auto / __src_netease / __src_kugou / __src_qq",
        "__src_kuwo / __src_apple / __src_deezer",
        "__click_ball（缩成悬浮球）/ __click_ball_hide（球点回主页面）",
        "__click_fav（收藏当前曲）/ __open_fav / __close_fav / __fav_row_<i>",
        "__open_loc（收藏页再拉出导入歌曲列表）/ __close_loc / __lyr_follow_on / __lyr_follow_off",
        "__toggle_ball_feature（设置里开关悬浮球，默认关）",
        "__lang_zh / __lang_en",
        "__set_dl_on / __set_dl_off / __toggle_dl（下载开关）",
        "__dl_current（下载正在播放的歌）",
        "__bgm_local（用已上传的本地视频当背景）/ __bgm_off（去掉背景媒体）",
        "__open_opt（打开优化页并测延迟）/ __opt_boost（优化网速）/ __opt_clean（清理）",
        "__opt_deep（清理含回收站）/ __opt_prio_normal / __opt_prio_above / __opt_prio_high / __close_opt",
    ],
}


# ---------------- 本地服务 ----------------
def send_json(handler, data, code=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        qs = urllib.parse.parse_qs(p.query)

        if path.startswith("/boost"):
            force_scan = (qs.get("scan") or [""])[0] == "1"
            if path == "/boost":
                opt_sys_probe()
                send_json(self, {"opt": OPT, "ext": OPT_EXT, "log": OPT_LOG[-12:],
                                 "scan": opt_scan() if force_scan else OPT_SCAN["data"],
                                 "lat": OPT_LAT["data"]})
                return
            if path == "/boost/scan":
                send_json(self, opt_scan(force=((qs.get("force") or [""])[0] == "1")))
                return
            if path == "/boost/lat":
                send_json(self, {"lat": opt_latency(force=True)})
                return
            if path == "/boost/timer":
                send_json(self, opt_timer((qs.get("on") or ["0"])[0]))
                return
            if path == "/boost/mem":
                send_json(self, opt_mem())
                return
            if path == "/boost/game":
                send_json(self, opt_game((qs.get("on") or ["0"])[0]))
                return
            if path == "/boost/power":
                send_json(self, opt_power((qs.get("plan") or ["balanced"])[0]))
                return
            if path == "/boost/iso":
                send_json(self, opt_iso((qs.get("on") or ["0"])[0]))
                return
            if path == "/boost/dwm":
                send_json(self, opt_dwm((qs.get("on") or ["0"])[0]))
                return
            if path == "/boost/admin":
                send_json(self, {"admin": opt_is_admin(), "elevate": (qs.get("go") or ["0"])[0] == "1"
                                 and opt_elevate()})
                return
            if path == "/boost/dev":
                if (qs.get("restore") or [""])[0] == "1":
                    send_json(self, opt_dev_restore())
                else:
                    send_json(self, {"admin": opt_is_admin(), "devs": opt_devices(
                        force=((qs.get("force") or [""])[0] == "1"))})
                return
            if path == "/boost/dev/msi":
                send_json(self, opt_dev_msi(int((qs.get("i") or ["-1"])[0]),
                                            int((qs.get("on") or ["1"])[0])))
                return
            if path == "/boost/dev/prio":
                send_json(self, opt_dev_prio(int((qs.get("i") or ["-1"])[0]),
                                             int((qs.get("v") or ["1"])[0])))
                return
            if path == "/boost/dev/aff":
                send_json(self, opt_dev_aff(int((qs.get("i") or ["-1"])[0]),
                                            (qs.get("m") or ["all"])[0]))
                return
            if path == "/boost/acc":
                send_json(self, opt_acc(int((qs.get("on") or ["0"])[0])))
                return
            if path == "/boost/rtss_install":
                send_json(self, opt_rtss_install())
                return
            if path == "/boost/rtss_page":
                try:
                    os.startfile(RTSS_PAGE)
                    send_json(self, {"ok": True, "opened": True})
                except Exception as e:
                    send_json(self, {"ok": False, "why": str(e)})
                return

            if path == "/boost/rtss":
                fps = (qs.get("fps") or [None])[0]
                send_json(self, opt_rtss(int(fps) if fps not in (None, "", "0") else None))
                return
            if path == "/boost/gamefiles":
                send_json(self, opt_gamefiles((qs.get("clean") or ["0"])[0] == "1"))
                return
            if path == "/boost/ifeo":
                if path == "/boost/ifeo":
                    if (qs.get("clear") or [""])[0] == "1":
                        send_json(self, opt_ifeo_clear())
                    else:
                        send_json(self, opt_ifeo((qs.get("exe") or [""])[0],
                                                 (qs.get("p") or ["high"])[0]))
                return
            if path == "/boost/net":
                net = opt_net()
                junk = opt_clean(["temp", "win_temp", "appcache", "logs"])
                net["junk"] = junk
                send_json(self, net)
                return
            if path == "/boost/clean":
                keys = (qs.get("items") or ["temp,appcache,logs"])[0].split(",")
                send_json(self, opt_clean(keys))
                return
            if path == "/boost/prio":
                send_json(self, opt_prio((qs.get("p") or ["normal"])[0]))
                return
            if path == "/boost/cores":
                send_json(self, opt_cores((qs.get("m") or ["all"])[0]))
                return
            if path == "/boost/bg":
                send_json(self, opt_bg(int((qs.get("l") or ["0"])[0])))
                return
            if path == "/boost/mode":
                OPT["mode"] = (qs.get("m") or ["normal"])[0]
                if OPT["mode"] == "comp":
                    OPT["prio"] = "high"
                    _set_proc_prio(os.getpid(), _PRIO_NAME["high"])
                elif OPT["mode"] == "normal":
                    OPT["prio"] = "normal"
                    _set_proc_prio(os.getpid(), _PRIO_NAME["normal"])
                opt_log("模式 -> %s" % OPT["mode"])
                send_json(self, {"ok": True, "opt": OPT})
                return
        if path == "/push":
            payload = (qs.get("cmd") or qs.get("q") or [""])[0].strip()
            if not payload:
                send_json(self, {"ok": False, "why": "empty"}, 400)
                return
            with PENDING_LOCK:
                PENDING.append(payload)
            log_api("queue push: %s" % payload)
            send_json(self, {"ok": True, "pending": len(PENDING)})
            return

        if path == "/next":
            with PENDING_LOCK:
                item = PENDING.pop(0) if PENDING else None
            if item:
                log_api("deliver: %s" % item)
            send_json(self, {"cmd": item})
            return

        if path == "/hb":
            with HB_LOCK:
                HB.update({
                    "t": (qs.get("t") or [""])[0],
                    "n": (qs.get("n") or ["0"])[0],
                    "m": (qs.get("m") or [""])[0],
                    "b": (qs.get("b") or [""])[0],
                    "f": (qs.get("f") or ["1"])[0],
                    "r": (qs.get("r") or ["1"])[0],
                    "pr": (qs.get("pr") or ["1"])[0],
                    "v": (qs.get("v") or ["0"])[0],
                    "vw": (qs.get("vw") or ["0"])[0],
                    "lang": (qs.get("lang") or ["zh"])[0],
                    "pref": (qs.get("pref") or ["auto"])[0],
                    "ord": (qs.get("ord") or ["-"])[0],
                    "sop": (qs.get("sop") or ["-"])[0],
                    "lv": (qs.get("lv") or [""])[0],
                    "mode": (qs.get("mode") or [""])[0],
                    "ln": (qs.get("ln") or ["0"])[0],
                    "lt": (qs.get("lt") or [""])[0],
                    "er": (qs.get("er") or [""])[0],
                    "du0": (qs.get("du0") or [""])[0],
                    "nm0": (qs.get("nm0") or [""])[0],
                    "qqn": (qs.get("qqn") or ["0"])[0],
                    "li": (qs.get("li") or ["-1"])[0],
                    "ld": (qs.get("ld") or ["0"])[0],
                    "ar0": (qs.get("ar0") or [""])[0],
                    "rm": (qs.get("rm") or ["0"])[0],
                    "rt": (qs.get("rt") or ["0"])[0],
                    "gone": (qs.get("gone") or ["0"])[0],
                    "bk": (qs.get("bk") or ["0"])[0],
                    "pin": (qs.get("pin") or ["0"])[0],
                    "fav": (qs.get("fav") or ["0"])[0],
                    "dl": (qs.get("dl") or ["0"])[0],
                    "bgm": (qs.get("bgm") or ["0"])[0],
                    "fvp": (qs.get("fvp") or ["0"])[0],
                    "ball": (qs.get("ball") or ["0"])[0],
                    "bf": (qs.get("bf") or ["0"])[0],
                    "bb": (qs.get("bb") or ["0"])[0],
                    "sn": (qs.get("sn") or [""])[0],
                    "sl": (qs.get("sl") or [""])[0],
                    "ea": (qs.get("ea") or ["0"])[0],
                    "srcn": (qs.get("srcn") or ["-1"])[0],
                    "pn": (qs.get("pn") or ["0"])[0],
                    "fvb": (qs.get("fvb") or ["0"])[0],
                    "star0": (qs.get("star0") or ["0"])[0],
                    "mt": (qs.get("mt") or [""])[0],
                    "fvline": (qs.get("fvline") or [""])[0],
                    "auto": (qs.get("auto") or [""])[0],
                    "smode": (qs.get("smode") or ["same"])[0],
                    "cfm": (qs.get("cfm") or ["0"])[0],
                    "favn": (qs.get("favn") or ["0"])[0],
                    "rs0": (qs.get("rs0") or [""])[0],
                    "br": (qs.get("br") or [""])[0],
                    "bc": (qs.get("bc") or [""])[0],
                    "bllr": (qs.get("bllr") or [""])[0],
                    "diag": (qs.get("diag") or [""])[0],
                    "bld": (qs.get("bld") or [""])[0],
                    "w": (qs.get("w") or ["0"])[0],
                    "h": (qs.get("h") or ["0"])[0],
                    "c": (qs.get("c") or ["0"])[0],
                    "d": (qs.get("d") or ["0"])[0],
                    "at": time.strftime("%H:%M:%S"),
                })
                HB_COUNT[0] += 1
                content_h = (qs.get("ch") or [""])[0]
            # 页面报来的内容高度和真实窗口不一致时，顺手校正（自愈，不靠一次性定时）
            if content_h:
                try:
                    resize_to(int(float(content_h)))
                except Exception:
                    pass
            # 只回用户自己按的「置顶」状态；置顶最高会把窗口真的变成 topmost，
            # 但绝不能拿窗口实际状态回填，否则开关会自己「开启」
            send_json(self, {"ok": True, "on_top": bool(TOP["on"]),
                             "topmost": bool(is_topmost(find_hwnd())),
                             "hard": bool(TOP.get("hard"))})
            return

        if path == "/copy":
            txt = (qs.get("text") or [""])[0]
            ok = set_clipboard(txt)
            log_api("copy -> %s (%s chars)" % (ok, len(txt)))
            send_json(self, {"ok": ok})
            return

        if path == "/ver":
            send_json(self, {"ok": True, "ver": APP_VER})
            return

        if path == "/update/check":
            send_json(self, update_check())
            return

        if path == "/update/run":
            send_json(self, update_run((qs.get("url") or [""])[0].strip()))
            return

        if path == "/local/list":
            send_json(self, {"ok": True, "rows": local_prune()})
            return

        if path == "/local/import":
            mode = (qs.get("mode") or ["copy"])[0]
            send_json(self, local_import("link" if mode == "link" else "copy"))
            return

        if path == "/cover/search":
            """给导入的本地歌曲找封面：按歌名问 QQ 音乐要元数据 + 专辑图"""
            q = (qs.get("q") or [""])[0].strip()
            rows = []
            if q:
                try:
                    for r in qq_search(q, 24):
                        if not r.get("cover"):
                            continue
                        rows.append({"name": r.get("name") or "", "artist": r.get("artist") or "",
                                     "album": r.get("album") or "", "cover": r.get("cover") or "",
                                     "dur": r.get("dur") or 0})
                except Exception as e:
                    log_api("cover search fail %r" % e)
            send_json(self, {"ok": True, "q": q, "rows": rows[:24]})
            return

        if path == "/local/cover":
            """点某个搜索结果：把它的专辑封面套到这首导入的歌上"""
            try:
                lid = int((qs.get("id") or ["0"])[0])
            except Exception:
                lid = 0
            url = (qs.get("url") or [""])[0]
            rows = local_load()
            hit = None
            for r in rows:
                if r.get("id") == lid:
                    hit = r
                    break
            if hit is None:
                send_json(self, {"ok": False, "why": "这首歌不在导入列表里"}, 404)
                return
            got = local_cover_save(lid, url)
            if not got:
                send_json(self, {"ok": False, "why": "封面没下下来"})
                return
            hit["cover"] = got
            hit["cover_src"] = url
            local_save(rows)
            log_api("local cover id=%d -> %s" % (lid, got))
            send_json(self, {"ok": True, "id": lid, "cover": got, "rows": rows})
            return

        if path == "/local/coverfile":
            try:
                lid = int((qs.get("id") or ["0"])[0])
            except Exception:
                lid = 0
            d = local_cover_dir()
            fp = ""
            try:
                for fn in os.listdir(d):
                    if fn.split(".")[0] == str(lid):
                        fp = os.path.join(d, fn)
                        break
            except Exception:
                pass
            if not fp or not os.path.isfile(fp):
                self.send_response(404)
                self.end_headers()
                return
            ext = os.path.splitext(fp)[1].lower()
            kind = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")
            with open(fp, "rb") as f:
                blob = f.read()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            self.wfile.write(blob)
            return

        if path == "/local/del":
            try:
                lid = int((qs.get("id") or ["0"])[0])
            except Exception:
                lid = 0
            rows = local_load()
            gone = [r for r in rows if r.get("id") == lid]
            rows = [r for r in rows if r.get("id") != lid]
            for g in gone:
                try:
                    p = g.get("path") or ""
                    # 只要文件在软件自己的音乐目录里，就一起删；别只看 mode 标签
                    if os.path.isfile(p):
                        try:
                            inside = os.path.dirname(os.path.abspath(p)) == os.path.abspath(LOCAL_MUSIC)
                        except Exception:
                            inside = False
                        if inside or g.get("mode") == "copy":
                            os.remove(p)
                except Exception:
                    pass
                try:
                    d = local_cover_dir()
                    for fn in os.listdir(d):
                        if fn.split(".")[0] == str(lid):
                            os.remove(os.path.join(d, fn))
                except Exception:
                    pass
            local_save(rows)
            send_json(self, {"ok": True, "rows": rows})
            return

        if path == "/local/file":
            try:
                lid = int((qs.get("id") or ["0"])[0])
            except Exception:
                lid = 0
            hit = None
            for r in local_load():
                if r.get("id") == lid:
                    hit = r
                    break
            fp = (hit or {}).get("path") or ""
            if not os.path.isfile(fp):
                self.send_response(404)
                self.end_headers()
                return
            size = os.path.getsize(fp)
            ext = os.path.splitext(fp)[1].lower()
            ctype = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
                     "wav": "audio/wav", "flac": "audio/flac", "ogg": "audio/ogg",
                     "opus": "audio/ogg", "wma": "audio/x-ms-wma",
                     "mp4": "video/mp4", "m4v": "video/mp4"}.get(ext.lstrip("."), "application/octet-stream")
            rng = self.headers.get("Range") or ""
            start, end = 0, size - 1
            part = False
            if rng.startswith("bytes="):
                try:
                    a, b = rng[6:].split("-")[:2]
                    if a:
                        start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                    part = True
                except Exception:
                    start, end, part = 0, size - 1, False
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            self.send_response(206 if part else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Access-Control-Allow-Origin", "*")
            if part:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return

        if path == "/stat":
            hwnd = find_hwnd()
            with HB_LOCK:
                data = dict(HB)
                data["polls"] = HB_COUNT[0]
            data["hwnd"] = bool(hwnd)
            data["rect"] = list(rect_of(hwnd)) if hwnd else None
            data["on_top"] = 1 if TOP["on"] else 0      # 用户自己按的置顶
            data["topmost"] = 1 if is_topmost(hwnd) else 0   # 窗口实际是不是 topmost（置顶最高也会让它变 1）
            data["top_hard"] = 1 if TOP.get("hard") else 0
            data["pending"] = len(PENDING)
            data["folded"] = HB.get("f")
            data["tray"] = 1 if TRAY["ok"] else 0
            data["multi"] = 1 if multi_on() else 0
            data["round"] = 1 if ROUND["px"] else 0
            data["roundpx"] = ROUND["px"]
            data["cbg"] = 1 if CLOSE_BG["on"] else 0
            send_json(self, data)
            return

        if path == "/toggle_top":
            # 以用户自己那个置顶开关为基准，别拿窗口实际 topmost 取反：
            # 开着「置顶最高」时窗口本来就是 topmost，取反会让第一次点击变成关闭
            want = not bool(TOP["on"])
            got = apply_top(want)
            send_json(self, {"ok": got is not None, "on_top": bool(TOP["on"]),
                             "topmost": bool(is_topmost(find_hwnd())),
                             "hard": bool(TOP.get("hard"))})
            return

        if path == "/cache":
            if "p" in qs:
                send_json(self, set_cache_dir((qs.get("p") or [""])[0]))
                return
            if "clear" in qs:
                send_json(self, cache_clear())
                return
            if "pick" in qs:
                got = pick_folder("选择缓存目录")
                if got:
                    r = set_cache_dir(got)
                    send_json(self, r)
                else:
                    send_json(self, {"ok": False, "why": "cancel"})
                return
            send_json(self, cache_info())
            return

        if path == "/opt":
            for key in ("ball_snap", "win_keep", "ball_top"):
                if key in qs:
                    set_opt(key, (qs.get(key) or ["0"])[0] in ("1", "true", "on"))
            if "snap_now" in qs or "keep_now" in qs:
                if BALL["on"]:
                    ball_snap(find_hwnd())
                keep_inside(find_hwnd())
            send_json(self, {"ok": True, "ball_snap": WOPT["ball_snap"],
                             "win_keep": WOPT["win_keep"],
                             "ball_top": WOPT["ball_top"]})
            return

        if path == "/autostart":
            if "on" in qs:
                want = (qs.get("on") or ["1"])[0] in ("1", "true", "True")
                r = autostart_set(want)
                r["cmd"] = autostart_cmd()
                send_json(self, r)
            else:
                st = autostart_state()
                st["ok"] = True
                st["cmd"] = autostart_cmd()
                send_json(self, st)
            return

        if path == "/set_top":
            if "hard" in qs:
                hon = (qs.get("hard") or ["0"])[0] in ("1", "true", "True")
                set_top_hard(hon)
                send_json(self, {"ok": True, "hard": hon,
                                 "on_top": bool(TOP["on"]),
                                 "topmost": bool(is_topmost(find_hwnd()))})
                return
            want = (qs.get("on") or ["1"])[0] in ("1", "true", "True")
            got = apply_top(want)
            send_json(self, {"ok": got is not None, "on_top": bool(TOP["on"]),
                             "topmost": bool(is_topmost(find_hwnd()))})
            return

        if path == "/move":
            try:
                dx = int(float((qs.get("dx") or ["0"])[0]))
                dy = int(float((qs.get("dy") or ["0"])[0]))
            except Exception:
                dx = dy = 0
            pos = move_by(dx, dy)
            send_json(self, {"ok": pos is not None, "pos": pos})
            return

        if path == "/drag":
            ok = drag_window((qs.get("ball") or [""])[0] in ("1", "true"))
            send_json(self, {"ok": ok})
            return

        if path == "/resize":
            h = (qs.get("h") or ["0"])[0]
            try:
                got = resize_to(int(float(h)))
            except Exception:
                got = None
            send_json(self, {"ok": got is not None, "height": got})
            return

        if path == "/kgurl":
            d = kugou_direct((qs.get("hash") or [""])[0])
            send_json(self, d or {"url": None})
            return

        if path == "/lyric":
            d = kugou_lyric((qs.get("hash") or [""])[0],
                            (qs.get("kw") or [""])[0],
                            (qs.get("dur") or ["0"])[0])
            send_json(self, d)
            return

        if path == "/srcs":
            send_json(self, {"srcs": src_catalog()})
            return

        if path == "/srcsearch":
            src = (qs.get("src") or ["auto"])[0]
            kw = (qs.get("q") or [""])[0]
            send_json(self, src_search(src, kw))
            return

        if path == "/fallback":
            q = (qs.get("q") or [""])[0]
            send_json(self, {"ok": True, "list": free_fallback(q)})
            return

        if path == "/qqsearch":
            w = (qs.get("w") or [""])[0]
            rows = qq_search(w)
            urls = qq_urls([r["id"] for r in rows[:12]]) if rows else {}
            for r in rows:
                u = urls.get(r["id"])
                r["play"] = 1 if u else 0
                if u:
                    r["url"] = u
            send_json(self, {"ok": True, "rows": rows})
            return

        if path == "/qqurl":
            mid = (qs.get("mid") or [""])[0]
            mm = (qs.get("mm") or [""])[0]
            send_json(self, {"ok": True, "url": qq_url(mid, mm) or ""})
            return

        if path == "/kwurl":
            rid = (qs.get("rid") or [""])[0]
            send_json(self, {"url": kuwo_url(rid)})
            return

        if path == "/ball":
            on = (qs.get("on") or ["1"])[0]
            try:
                got = set_ball(on, (qs.get("h") or ["0"])[0], (qs.get("w") or ["0"])[0])
            except Exception:
                got = None
            send_json(self, {"ok": got is not None, "size": got})
            return

        if path == "/proxy":
            u = (qs.get("u") or [""])[0]
            if not u.startswith("http"):
                send_json(self, {"ok": False, "why": "bad url"}, 400)
                return
            try:
                body, ctype = http_proxy(u)
            except Exception as e:
                send_json(self, {"ok": False, "why": repr(e)[:120]}, 502)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/download":
            u = (qs.get("url") or [""])[0]
            nm = (qs.get("name") or [""])[0]
            ex = (qs.get("ext") or [""])[0]
            if not u.startswith("http"):
                send_json(self, {"ok": False, "why": "没有可下载的直链"}, 400)
                return
            try:
                got = do_download(u, nm, ex)
            except Exception as e:
                log_api("download fail: %r" % e)
                send_json(self, {"ok": False, "why": repr(e)[:120]}, 502)
                return
            log_api("download ok: %s" % got)
            send_json(self, {"ok": True, "path": got, "dir": dl_dir()})
            return

        if path == "/dlpath":
            want = (qs.get("p") or [""])[0]
            if want:
                got = set_dl_dir(want)
                if not got:
                    send_json(self, {"ok": False, "why": "这个路径用不了", "dir": dl_dir()}, 400)
                    return
                log_api("dl dir -> %s" % got)
                send_json(self, {"ok": True, "dir": got})
                return
            send_json(self, {"dir": dl_dir()})
            return

        if path == "/dlpick":
            got = pick_folder()
            if got:
                saved = set_dl_dir(got)
                log_api("dlpick -> %s" % saved)
                send_json(self, {"ok": True, "dir": saved or got})
            else:
                send_json(self, {"ok": False, "why": "cancelled", "dir": dl_dir()})
            return

        if path == "/openfolder":
            try:
                folder = (qs.get("p") or [""])[0] or dl_dir()
                if os.path.isdir(folder):
                    os.startfile(folder)
                send_json(self, {"ok": True})
            except Exception as e:
                send_json(self, {"ok": False, "why": repr(e)[:80]}, 500)
            return

        if path == "/bgmedia":
            """背景视频 / 音频：从本地缓存文件流出去，带 Range"""
            fn = os.path.join(BG_DIR, "bg.bin")
            meta = os.path.join(BG_DIR, "bg.json")
            if not os.path.exists(fn):
                send_json(self, {"ok": False, "why": "none"}, 404)
                return
            try:
                kind = "video/mp4"
                if os.path.exists(meta):
                    with open(meta, encoding="utf-8") as f:
                        kind = json.load(f).get("mime") or kind
            except Exception:
                pass
            size = os.path.getsize(fn)
            rng = self.headers.get("Range") or ""
            start, end = 0, size - 1
            m = re.match(r"bytes=([0-9]+)-([0-9]*)", rng)
            if m:
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", kind)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if rng:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            with open(fn, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return

        if path == "/api":
            send_json(self, API_DOC)
            return

        if path == "/round":
            if "px" in qs:
                try:
                    ROUND["px"] = max(0, min(40, int((qs.get("px") or ["0"])[0])))
                except Exception:
                    ROUND["px"] = 0
            elif "lv" in qs:
                try:
                    ROUND["px"] = {0: 0, 1: 10, 2: 18, 3: 28}.get(int((qs.get("lv") or ["0"])[0]), 0)
                except Exception:
                    ROUND["px"] = 0
            st = load_state()
            st["round_px"] = ROUND["px"]
            save_state(st)
            set_round_win(find_hwnd(), ROUND["px"])
            log_api("round px -> %d" % ROUND["px"])
            send_json(self, {"ok": True, "px": ROUND["px"], "on": 1 if ROUND["px"] else 0})
            return

        if path == "/multi":
            qs = urllib.parse.parse_qs(p.query)
            if "on" in qs:
                set_multi((qs.get("on") or ["1"])[0] in ("1", "true", "on"))
            send_json(self, {"ok": True, "multi": 1 if multi_on() else 0, "pid": os.getpid()})
            return

        if path == "/tray":
            qs = urllib.parse.parse_qs(p.query)
            if "bg" in qs:
                set_close_bg((qs.get("bg") or ["1"])[0] in ("1", "true", "on"))
            send_json(self, {"ok": True, "bg": tray_bg(), "tray": bool(TRAY["ok"])})
            return

        if path == "/tray/show":
            send_json(self, {"ok": show_main(), "bg": tray_bg()})
            return

        if path == "/tray/exit":
            send_json(self, {"ok": True})
            threading.Thread(target=hard_exit, daemon=True).start()
            return

        if path == "/xclose":
            # 页面右上角那个 × ：按「窗口 · 关闭」的设置走
            if tray_bg():
                try:
                    WINDOW["w"].hide()
                except Exception as e:
                    log_api("xclose hide fail %r" % e)
                    send_json(self, {"ok": True, "action": "exit"})
                    threading.Thread(target=hard_exit, daemon=True).start()
                    return
                send_json(self, {"ok": True, "action": "tray"})
                log_api("xclose -> tray")
            else:
                send_json(self, {"ok": True, "action": "exit"})
                threading.Thread(target=hard_exit, daemon=True).start()
            return

        if path == "/close":
            send_json(self, {"ok": True})
            def bye():
                HARD_EXIT["v"] = True
                time.sleep(0.15)
                try:
                    WINDOW["w"].destroy()
                except Exception:
                    pass
                time.sleep(0.6)
                os._exit(0)
            threading.Thread(target=bye, daemon=True).start()
            return

        return super().do_GET()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path not in ("/bgup", "/txt"):
            self.send_response(404)
            self.end_headers()
            return
        if p.path == "/txt":
            # 歌词导出：body 是纯文本，落到下载目录
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln) if ln else b""
                if not raw:
                    send_json(self, {"ok": False, "why": "empty"}, 400)
                    return
                qs = urllib.parse.parse_qs(p.query)
                name = safe_name((qs.get("name") or ["歌词"])[0])
                folder = dl_dir()
                dst = os.path.join(folder, name + ".txt")
                base, n = dst, 1
                while os.path.exists(dst):
                    dst = os.path.join(folder, "%s (%d).txt" % (name, n))
                    n += 1
                txt = raw.decode("utf-8", "ignore")
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(txt)
                log_api("txt save %s %d bytes" % (dst, len(txt)))
                send_json(self, {"ok": True, "path": dst, "bytes": len(txt)})
            except Exception as e:
                send_json(self, {"ok": False, "why": repr(e)[:100]}, 500)
            return
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(ln) if ln else b""
            ctype = self.headers.get("Content-Type") or ""
            # 原始文件体（页面直接发 blob，不带 multipart）
            if not raw:
                send_json(self, {"ok": False, "why": "empty"}, 400)
                return
            if not os.path.isdir(BG_DIR):
                os.makedirs(BG_DIR, exist_ok=True)
            with open(os.path.join(BG_DIR, "bg.bin"), "wb") as f:
                f.write(raw)
            mime = ctype if ctype.startswith(("video/", "audio/")) else (
                "video/mp4" if raw[:4] == b"\x00\x00\x00\x18" or b"ftyp" in raw[:64] else "audio/mpeg")
            with open(os.path.join(BG_DIR, "bg.json"), "w", encoding="utf-8") as f:
                json.dump({"mime": mime, "bytes": len(raw)}, f)
            log_api("bg up %d bytes %s" % (len(raw), mime))
            send_json(self, {"ok": True, "url": "/bgmedia", "mime": mime, "bytes": len(raw)})
        except Exception as e:
            send_json(self, {"ok": False, "why": repr(e)[:100]}, 500)


def _port_busy(port):
    """Windows 上 SO_REUSEADDR 会让第二个实例『绑上』已占的端口，先探一下"""
    if not port:
        return False
    try:
        s = socket.create_connection(("127.0.0.1", port), 0.4)
        s.close()
        return True
    except OSError:
        return False


def start_server():
    for port in (8777, 8778, 8779, 0):
        if _port_busy(port):
            log_api("port %d busy, skip" % port)
            continue
        try:
            srv = http.server.ThreadingHTTPServer(
                ("127.0.0.1", port), functools.partial(Handler, directory=WEB))
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(os.path.join(LOCAL, "port_%d.txt" % os.getpid()), "w", encoding="utf-8") as pf:
                    pf.write(str(srv.server_address[1]))
            except Exception:
                pass
            if not MULTI["on"] or not os.path.exists(PORT_FILE):
                with open(PORT_FILE, "w", encoding="utf-8") as f:
                    f.write(str(srv.server_address[1]))
        except Exception:
            pass
        return srv
    return None


def main():
    st0 = load_state()
    if "close_bg" in st0:
        CLOSE_BG["on"] = bool(st0.get("close_bg"))
    if "multi" in st0:
        MULTI["on"] = bool(st0.get("multi"))
    try:
        ROUND["px"] = max(0, min(40, int(st0.get("round_px") or 0)))
    except Exception:
        ROUND["px"] = 0
    # 开机自启：上次用户开过，但注册表被清理软件抹了，就自己补回去
    try:
        if bool(st0.get("auto_boot")) and not autostart_state().get("on"):
            autostart_set(True)
            save_state({"auto_boot": True})
            log_api("autostart self-heal")
    except Exception as e:
        log_api("autostart heal fail %r" % e)
    if not claim_single():
        log_api("another instance is alive, woke it up and exit")
        time.sleep(0.15)
        os._exit(0)
    srv = start_server()
    if srv is None:
        raise SystemExit("本地端口起不来")
    port = srv.server_address[1]
    url = "http://127.0.0.1:%d/index.html?v=%d" % (port, int(time.time()))

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        payload = json.dumps({"from": "豆包", "title": args[0], "note": "命令行叫醒的"},
                             ensure_ascii=False)
        url += "#cmd=" + urllib.parse.quote(payload, safe="")

    import webview  # 延迟导入，缩短冷启动

    win = webview.create_window(APP_TITLE, url, width=WIN_W, height=WIN_H,
                                frameless=True, easy_drag=False, resizable=False,
                                min_size=(40, 40), on_top=False, transparent=False,
                                background_color="#000000")
    WINDOW["w"] = win

    def on_closing():
        if HARD_EXIT["v"]:
            return True
        if CLOSE_BG["on"]:
            try:
                win.hide()
            except Exception as e:
                log_api("hide fail %r" % e)
                return True
            log_api("close -> tray")
            return False        # 取消关闭，留在托盘里继续播
        return True

    try:
        win.events.closing += on_closing
    except Exception as e:
        log_api("closing hook fail %r" % e)

    def after_start():
        time.sleep(1.2)
        if TOP["on"] or TOP["hard"]:
            raw_top(True)
        # 球外面那圈白来自 Form 的系统灰底。给窗口开全客户区玻璃，
        # webview 的默认底色（这里设成纯黑）就会透成桌面，且不影响鼠标。
        try:
            hwnd = None
            for _ in range(90):
                hwnd = find_hwnd()
                if hwnd:
                    break
                time.sleep(0.1)
            if hwnd:
                set_round_region(hwnd, 0)
                sync_round(hwnd)
                log_api("startup region cleared")
            else:
                log_api("glass fail: no hwnd")
        except Exception as e:
            log_api("glass fail: %r" % e)
        log_api("window up, port=%d" % port)

    threading.Thread(target=after_start, daemon=True).start()
    tray_start()
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Thread(target=hard_top_loop, daemon=True).start()
    if MULTI["on"]:
        # 多开时各用各的 WebView2 数据目录，否则第二个实例会被同一个 profile 卡住
        webview.start(debug=False, private_mode=False,
                      storage_path=os.path.join(LOCAL, "profile_%d" % os.getpid()))
    else:
        webview.start(debug=False, private_mode=False)
    try:
        if OPT_EXT.get("timer"):
            ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


if __name__ == "__main__":
    main()
