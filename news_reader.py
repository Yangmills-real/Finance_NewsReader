#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
 财经新闻阅读悬浮窗（报纸风）  News Reader
================================================================
 功能：
   - 无边框、始终置顶、可鼠标拖拽
   - 羊皮纸底色 + 衬线字体（宋体）+ 深墨色文字，报纸排版观感
   - 通过 paramiko 的 Transport 长连接读服务器上的 daily_news.txt
     （只在启动时握手认证一次，之后每 5 分钟复用同一条连接，
       不会反复登录去触发 Fail2ban）
   - tk.Text 只读显示，滚轮可滚动；禁止选中/复制，这样在正文上按住就能拖窗口
   - ⬇/📌 一键沉底（SetWindowPos + HWND_BOTTOM，真正的 Z 序最底）
     + 全局热键 Ctrl+Shift+3 唤醒（RegisterHotKey，沉底后唯一的找回方式）
   - 右键菜单：立即刷新 / 沉底置顶 / 退出

 运行环境：Windows + Python 3.8 以上
 安装依赖：pip install paramiko
 远程文件：由 config.json 的 news_path 指定
================================================================
"""

import os
import queue
import shlex
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import messagebox, simpledialog

# ------------------------------------------------------------------
# 导入 paramiko
# ------------------------------------------------------------------
# ★ 这里【故意】用 except Exception 而不是 except ImportError：
#   打包后最常出问题的不是"没安装"，而是 cryptography 的 DLL 加载失败，
#   那种情况抛的是 OSError（DLL load failed），用 ImportError 根本接不住。
#   而且必须把真实异常显示出来，否则只会误导成"没装 paramiko"。
try:
    import paramiko
except Exception as _exc:                  # noqa: BLE001
    import traceback
    _detail = traceback.format_exc()

    # 把完整堆栈写到 exe（或脚本）同目录，方便事后排查
    try:
        _base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                 else os.path.dirname(os.path.abspath(__file__)))
        _log_path = os.path.join(_base, "import_error.log")
        with open(_log_path, "w", encoding="utf-8") as _f:
            _f.write(_detail)
    except Exception:
        _log_path = "(日志写入失败)"

    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "依赖加载失败",
        f"paramiko 存在但导入失败。\n\n"
        f"真实原因：{type(_exc).__name__}: {_exc}\n\n"
        f"完整堆栈已写入：\n{_log_path}\n\n"
        f"常见原因：\n"
        f"  · 打包缓存没清（删掉 build/dist 重新打包）\n"
        f"  · 系统缺 Visual C++ 运行库\n"
        f"  · 打包时漏了 --collect-all cryptography")
    sys.exit(1)


# ==================================================================
# 窗口沉底 / 置顶（纯 ctypes 调 Windows API，不需要第三方库）
# ==================================================================
# Windows 下光靠 root.lower() 往往压不到最底（尤其在无边框窗口上），
# 必须直接调 SetWindowPos 并传 HWND_BOTTOM 才行。
import ctypes

IS_WINDOWS = sys.platform == "win32"

_HWND_TOP = 0            # 放到 Z 序最顶（普通层）
_HWND_BOTTOM = 1         # 放到 Z 序最底（壁纸之上、其它窗口之下）
_HWND_TOPMOST = -1       # 放到置顶层最顶

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040

_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,                  # hWnd
        ctypes.c_void_p,                  # hWndInsertAfter
        ctypes.c_int, ctypes.c_int,       # X, Y
        ctypes.c_int, ctypes.c_int,       # cx, cy
        ctypes.c_uint,                    # uFlags
    ]
    _user32.SetWindowPos.restype = ctypes.c_int

    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short

    # 64 位系统用 GetWindowLongPtrW，32 位只有 GetWindowLongW
    try:
        _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        _get_window_style = _user32.GetWindowLongPtrW
    except AttributeError:
        _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.GetWindowLongW.restype = ctypes.c_long
        _get_window_style = _user32.GetWindowLongW
else:
    _user32 = None
    _get_window_style = None


def _hwnd_value(signed):
    """
    HWND_TOP(0) / HWND_BOTTOM(1) / HWND_TOPMOST(-1) 是"伪句柄"，
    -1 必须按指针宽度转成无符号（64 位下是 0xFFFFFFFFFFFFFFFF），
    否则 SetWindowPos 会把它当成无效值。
    """
    bits = 8 * ctypes.sizeof(ctypes.c_void_p)
    return signed & ((1 << bits) - 1)


def get_hwnd(root):
    """取 Tk 顶层窗口真正的 HWND（无边框窗口下 wm_frame 就是它）"""
    root.update_idletasks()
    try:
        return int(root.wm_frame(), 16)     # 返回形如 '0x1a2b3c' 的字符串
    except Exception:
        return root.winfo_id()              # 兜底：Tk 自己的窗口句柄


def win_set_pos(hwnd, insert_after, flags):
    """调用 SetWindowPos，成功返回 True"""
    if not IS_WINDOWS:
        return False
    return bool(_user32.SetWindowPos(hwnd, _hwnd_value(insert_after),
                                     0, 0, 0, 0, flags))


def win_is_topmost(hwnd):
    """窗口当前是否带 WS_EX_TOPMOST 样式（自测/排错用）"""
    if not IS_WINDOWS:
        return False
    return bool(_get_window_style(hwnd, _GWL_EXSTYLE) & _WS_EX_TOPMOST)


def key_state(vk):
    """
    读取按键的原始状态（全局，窗口没焦点也能读）。
      bit 15 (0x8000) = 当前正按着
      bit 0  (0x0001) = 自上次查询以来按下过（读一次就清零）
    """
    if not IS_WINDOWS:
        return 0
    return _user32.GetAsyncKeyState(vk)


def key_down(vk):
    """键当前是否按着"""
    return bool(key_state(vk) & 0x8000)


def parse_hotkey(spec):
    """把 'ctrl+shift+3' 解析成一串虚拟键码（VK），轮询兜底用"""
    mods = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
    vks = []
    for part in str(spec).lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in mods:
            vks.append(mods[part])
        elif len(part) == 1 and part.isdigit():
            vks.append(0x30 + int(part))            # '3' -> 0x33
        elif len(part) == 1 and "a" <= part <= "z":
            vks.append(ord(part.upper()))
        else:
            raise ValueError(f'快捷键格式不对：{part!r}（示例 "ctrl+shift+3"）')
    return vks


# ---- RegisterHotKey：系统级注册，按下瞬间由系统投递消息，100% 不会漏 ----
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 0x5A19

if IS_WINDOWS:
    from ctypes import wintypes
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    _user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_uint, ctypes.c_uint]
    _user32.RegisterHotKey.restype = ctypes.c_int
    _user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.UnregisterHotKey.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_void_p]
    _user32.PostThreadMessageW.restype = ctypes.c_int


def parse_hotkey_win(spec):
    """把 'ctrl+shift+3' 解析成 (修饰键掩码, 主键VK)，给 RegisterHotKey 用"""
    mask, vk = 0, None
    table = {"ctrl": _MOD_CONTROL, "shift": _MOD_SHIFT,
             "alt": _MOD_ALT, "win": _MOD_WIN}
    for part in str(spec).lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in table:
            mask |= table[part]
        elif len(part) == 1 and part.isdigit():
            vk = 0x30 + int(part)
        elif len(part) == 1 and "a" <= part <= "z":
            vk = ord(part.upper())
        else:
            raise ValueError(f'快捷键格式不对：{part!r}（示例 "ctrl+shift+3"）')
    if vk is None:
        raise ValueError("快捷键必须包含一个字母或数字主键")
    return mask, vk


class HotkeyWatcher(threading.Thread):
    """
    在独立线程里用 RegisterHotKey 注册系统级热键。

    为什么不用轮询 GetAsyncKeyState：
      实测发现 Ctrl/Shift 这类修饰键的"自上次查询以来按下过"低位会被系统
      自己消费掉 —— 只有主键可靠。窗口沉底、按得又快时，轮询可能整个漏掉。
      RegisterHotKey 由系统在按键瞬间投递 WM_HOTKEY，不存在漏检。
    """

    def __init__(self, spec, out_queue):
        super().__init__(daemon=True)
        self.spec = spec
        self.out_queue = out_queue
        self.ok = False             # 注册成功才为 True
        self.error = None
        self._tid = None
        self._stopping = False

    def run(self):
        if not IS_WINDOWS:
            return
        try:
            mask, vk = parse_hotkey_win(self.spec)
        except Exception as exc:
            self.error = str(exc)
            print(f"[警告] {self.error}")
            return
        self._tid = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, _HOTKEY_ID,
                                      mask | _MOD_NOREPEAT, vk):
            self.error = (f"热键 {self.spec} 注册失败，多半被其它程序占用了，"
                          f"改用按键轮询兜底")
            print(f"[提示] {self.error}")
            return
        self.ok = True
        msg = wintypes.MSG()
        try:
            while not self._stopping:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self.out_queue.put(True)      # 交给主线程去切换
        finally:
            _user32.UnregisterHotKey(None, _HOTKEY_ID)

    def stop(self):
        """取消注册并让阻塞中的 GetMessage 返回"""
        self._stopping = True
        if self._tid and IS_WINDOWS:
            _user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)


# ============================================================
# ============================================================
#  0. 配置文件（开源版：服务器地址 / 端口 / 用户名 / 私钥一律不写死在代码里）
# ============================================================
#  配置存放位置：
#    Windows  %APPDATA%\\NewsReader\\config.json
#    Linux    ~/.config/NewsReader/config.json
#    macOS    ~/Library/Application Support/NewsReader/config.json
#  首次运行会弹图形向导；想重填就删掉这个文件再开程序。
# ============================================================
import json as _json

CONFIG_APP = "NewsReader"

DEFAULT_CONFIG = {
    "host": "",                    # 服务器地址（IP 或域名）
    "port": 22,                    # SSH 端口
    "user": "",                    # 登录用户名
    "key_file": "",                # 私钥文件【绝对路径】
    "news_path": "/root/daily_news.txt",  # 服务器上要读取的文件
    "refresh_sec": 300,            # 刷新间隔（秒）
    "hotkey": "ctrl+shift+n",      # 全局快捷键；"" = 不启用
}


def config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, CONFIG_APP)


def config_path():
    return os.path.join(config_dir(), "config.json")


def load_config():
    """读配置；文件不存在/损坏都回默认值，绝不抛异常"""
    data = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), encoding="utf-8") as f:
            raw = _json.load(f)
        if isinstance(raw, dict):
            for k in DEFAULT_CONFIG:
                if k in raw:
                    data[k] = raw[k]
    except (OSError, ValueError):
        pass
    return data


def save_config(cfg):
    """原子写入（先写 .tmp 再替换）"""
    try:
        os.makedirs(config_dir(), exist_ok=True)
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path())
        return True
    except OSError as exc:
        print("[配置] 保存失败：%s" % exc)
        return False


def config_complete(cfg):
    """缺关键项就得弹向导"""
    key = cfg.get("key_file") or ""
    return bool(cfg.get("host")) and bool(cfg.get("user")) and bool(key) and os.path.exists(key)


def normalize_hotkey(spec):
    """'Ctrl + Shift + M' -> 'ctrl+shift+m'；'' = 不启用；None = 格式非法"""
    if spec is None:
        return ""
    s = str(spec).strip().lower().replace(" ", "")
    if not s:
        return ""
    alias = {"control": "ctrl", "super": "win", "meta": "win", "option": "alt"}
    mods, key = [], None
    for p in s.split("+"):
        if not p:
            continue
        p = alias.get(p, p)
        if p in ("ctrl", "shift", "alt", "win"):
            if p not in mods:
                mods.append(p)
        elif len(p) == 1 and p.isalnum():
            key = p
        else:
            return None
    if key is None:
        return None
    return "+".join([m for m in ("ctrl", "shift", "alt", "win") if m in mods] + [key])


def hotkey_display(spec):
    if not spec:
        return "（未启用）"
    names = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win"}
    return "+".join(names.get(p, p.upper()) for p in spec.split("+"))


def apply_config(cfg):
    """把配置写回模块级常量（改完立刻生效）"""
    g = globals()
    g["HOST"] = (cfg.get("host") or "").strip()
    try:
        g["PORT"] = int(cfg.get("port") or 22)
    except (TypeError, ValueError):
        g["PORT"] = 22
    g["USER"] = (cfg.get("user") or "").strip()
    keyf = (cfg.get("key_file") or "").strip()
    g["KEY_FILE_HINT"] = keyf
    g["KEY_NAME"] = os.path.basename(keyf) if keyf else ""
    g["NEWS_PATH"] = (cfg.get("news_path") or "/root/daily_news.txt").strip()
    try:
        g["REFRESH_SEC"] = max(30, int(cfg.get("refresh_sec") or 300))
    except (TypeError, ValueError):
        g["REFRESH_SEC"] = 300
    hk = normalize_hotkey(cfg.get("hotkey", DEFAULT_CONFIG["hotkey"]))
    g["HOTKEY"] = DEFAULT_CONFIG["hotkey"] if hk is None else hk


def run_config_wizard(root, cfg):
    """图形配置向导（首次运行/配置不全时）。返回新配置 dict，取消返回 None。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    win = tk.Toplevel(root)
    win.title("NewsReader · 首次配置")
    win.resizable(False, False)
    win.attributes("-topmost", True)

    cur = dict(DEFAULT_CONFIG)
    cur.update(cfg or {})

    var_host = tk.StringVar(value=cur.get("host", ""))
    var_port = tk.StringVar(value=str(cur.get("port", 22)))
    var_user = tk.StringVar(value=cur.get("user", ""))
    var_key = tk.StringVar(value=cur.get("key_file", ""))
    var_iface = tk.StringVar(value=cur.get("iface", "eth0"))
    var_refresh = tk.StringVar(value=str(cur.get("refresh_sec", 300)))
    hk0 = normalize_hotkey(cur.get("hotkey", ""))
    var_hot = tk.StringVar(value=hotkey_display(DEFAULT_CONFIG["hotkey"] if hk0 is None else hk0))
    state = {"capturing": False, "hotkey": cur.get("hotkey", DEFAULT_CONFIG["hotkey"])}

    pad = {"padx": 10, "pady": 5}
    rows = [("服务器地址", var_host, False), ("SSH 端口", var_port, False),
            ("用户名", var_user, False), ("私钥文件", var_key, True),
            ("远程文件", var_iface, False), ("刷新间隔(秒)", var_refresh, False)]
    first = None
    for i, (text, var, is_file) in enumerate(rows):
        tk.Label(win, text=text, anchor="e", width=12).grid(row=i, column=0, sticky="e", **pad)
        ent = tk.Entry(win, textvariable=var, width=34)
        ent.grid(row=i, column=1, sticky="we", **pad)
        if first is None:
            first = ent
        if is_file:
            def browse(_e=None, v=var):
                p = filedialog.askopenfilename(title="选择 SSH 私钥文件",
                                               initialdir=os.path.expanduser("~/.ssh"))
                if p:
                    v.set(os.path.normpath(p))
            tk.Button(win, text="浏览…", command=browse, width=6).grid(
                row=i, column=2, sticky="w", padx=(0, 10), pady=5)

    r = len(rows)
    tk.Label(win, text="全局快捷键", anchor="e", width=12).grid(row=r, column=0, sticky="e", **pad)
    ent_hk = tk.Entry(win, textvariable=var_hot, width=34, state="readonly",
                      readonlybackground="#eeeeee", justify="center")
    ent_hk.grid(row=r, column=1, columnspan=2, sticky="we", **pad)

    def begin_capture(_e=None):
        state["capturing"] = True
        var_hot.set("按下组合键…")
        ent_hk.focus_set()
        return "break"

    def on_key(event):
        if not state["capturing"]:
            return "break"
        ks = event.keysym
        if ks in ("Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R",
                  "Win_L", "Win_R", "Super_L", "Super_R", "Caps_Lock"):
            return "break"
        if ks == "Escape":
            state["capturing"] = False
            var_hot.set(hotkey_display(state["hotkey"]))
            return "break"
        if ks in ("BackSpace", "Delete"):
            state["capturing"] = False
            state["hotkey"] = ""
            var_hot.set(hotkey_display(""))
            return "break"
        mods = []
        if event.state & 0x0004:
            mods.append("ctrl")
        if event.state & 0x0001:
            mods.append("shift")
        if event.state & 0x0008 or event.state & 0x20000:
            mods.append("alt")
        k = ks.lower()
        if len(k) != 1 or not k.isalnum() or not mods:
            return "break"
        state["hotkey"] = normalize_hotkey("+".join(mods + [k])) or ""
        state["capturing"] = False
        var_hot.set(hotkey_display(state["hotkey"]))
        return "break"

    ent_hk.bind("<Button-1>", begin_capture)
    ent_hk.bind("<KeyPress>", on_key)

    out = {"cfg": None}

    def on_ok():
        host, user, keyf = var_host.get().strip(), var_user.get().strip(), var_key.get().strip()
        if not host or not user or not keyf:
            messagebox.showwarning("还差一点", "服务器地址、用户名、私钥文件都必须填写。", parent=win)
            return
        if not os.path.exists(keyf):
            messagebox.showwarning("找不到私钥", "这个文件不存在：\n%s" % keyf, parent=win)
            return
        try:
            port = int(var_port.get())
        except ValueError:
            port = 22
        try:
            refresh = max(30, int(var_refresh.get()))
        except ValueError:
            refresh = 300
        out["cfg"] = {"host": host, "port": port, "user": user,
                      "key_file": os.path.normpath(keyf),
                      "news_path": var_iface.get().strip() or "/root/daily_news.txt",
                      "refresh_sec": refresh,
                      "hotkey": state["hotkey"] if state["hotkey"] is not None
                      else DEFAULT_CONFIG["hotkey"]}
        win.destroy()

    def on_cancel():
        out["cfg"] = None
        win.destroy()

    btns = tk.Frame(win)
    btns.grid(row=r + 1, column=0, columnspan=3, pady=(8, 12))
    tk.Button(btns, text="保存并运行", width=14, command=on_ok).pack(side="left", padx=6)
    tk.Button(btns, text="退出", width=8, command=on_cancel).pack(side="left", padx=6)

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("+%d+%d" % (max(0, (sw - win.winfo_reqwidth()) // 2),
                             max(0, (sh - win.winfo_reqheight()) // 3)))
    if first is not None:
        first.focus_set()
    win.grab_set()
    root.wait_window(win)
    return out["cfg"]


# ============================================================
#  1. 配置区
# ============================================================

# ---- 服务器（★ 具体值来自 config.json，源码里不写死）----
_CFG = load_config()
HOST = _CFG.get("host", "")
try:
    PORT = int(_CFG.get("port") or 22)
except (TypeError, ValueError):
    PORT = 22
USER = _CFG.get("user", "")
KEY_FILE_HINT = _CFG.get("key_file", "")
KEY_NAME = os.path.basename(KEY_FILE_HINT) if KEY_FILE_HINT else ""

# ---- 要读的文件 ----
NEWS_PATH = _CFG.get("news_path", "/root/daily_news.txt")

# ---- 刷新与超时 ----
REFRESH_SEC = max(30, int(_CFG.get("refresh_sec") or 300))  # 刷新间隔（秒）
CONNECT_TIMEOUT = 10       # SSH 建连 / 认证超时
CMD_TIMEOUT = 20           # 读文件超时
KEEPALIVE_SEC = 30         # SSH 心跳，防止长连接被中间设备掐断
MAX_BACKOFF_SEC = 900      # 连续失败时最长退避到 15 分钟

# ---- 报纸风配色 ----
PAPER_BG = "#f4f1ea"       # 羊皮纸底色（窗口外框）
TEXT_BG = "#f9f6f0"        # 正文纸面（略亮一点，做出层次）
INK = "#2b2b2b"            # 墨黑正文
INK_TITLE = "#1a1a1a"      # 标题更深
INK_DIM = "#6f6a61"        # 次要信息（来源/时间）
RULE = "#3a352e"           # 深色分割线
BORDER = "#d9d2c5"         # 极淡的外描边
STATUS_OK = "#3f7d4f"
STATUS_WARN = "#b8862b"
STATUS_BAD = "#a33a34"

ALPHA = 0.96               # 轻微透明 ≈"磨砂感"（tkinter 做不了真高斯模糊）

# ---- ★ 置顶 / 沉底 ----
# 全局快捷键：窗口没焦点、已沉底时也能用来唤醒。
# ★ 你的三个程序必须用不同的键，否则同一个键会被先启动的那个独占：
#     时钟 Ctrl+Shift+1 / 流量监控 Ctrl+Shift+2 / 本程序 Ctrl+Shift+3
_hk = normalize_hotkey(_CFG.get("hotkey", DEFAULT_CONFIG["hotkey"]))
HOTKEY = DEFAULT_CONFIG["hotkey"] if _hk is None else _hk
PIN_TEXT_UP = "⬇"              # 置顶状态下按钮显示这个（点它 → 沉底）
PIN_TEXT_DOWN = "📌"            # 沉底状态下显示这个（点它 → 置顶）
HOTKEY_POLL_MS = 80            # 快捷键轮询间隔（毫秒）
# 如果 ⬇ / 📌 显示成方框，改成纯文字即可：PIN_TEXT_UP = "沉" / PIN_TEXT_DOWN = "顶"

# ---- 文本框尺寸（单位是像素，不是字符）----
# 注意：tk.Text 的 width/height 参数是"字符数"，写 560 会得到约 4000px 宽的怪物。
# 所以这里用像素：外面套一个固定尺寸的 Frame，Text 填满它。
TEXT_W, TEXT_H = 560, 560
# 想再大/再小就改这两个数。实测：12pt 宋体下 442 字约占 650px 高，
# 当前 560px 约能一眼看到 85%；想完全不滚动就把 TEXT_H 设到 660。
# 同时嫌窗口太大，可以把字号从 12 调回 11（见 __init__ 里的 self.f_body）。

# ---- 初始位置 ----
# 放在屏幕左侧：右上角已经有你的时钟和流量监控了，别叠在一起
START_X = 60
START_Y = 140

# ---- 字体（在运行时从系统已安装字体里挑，见 pick_serif_font）----
SERIF_CANDIDATES = ["宋体", "SimSun", "Georgia", "Times New Roman", "Times"]
SERIF_FALLBACK = "Times"


# ============================================================
#  2. 字体探测
# ============================================================
def pick_serif_font(candidates, fallback):
    """
    按顺序挑第一个系统里真实存在的衬线字体。

    ★ 为什么需要这个：同一个字体在不同 Windows 上注册名不一样。
      这台机器上有「宋体」，但没有「SimSun」（同一字体的英文名）。
      若代码里硬写 "SimSun"，Tk 不会报错，而是【静默回退】成默认字体，
      排版立刻变得很难看且很难排查。
    """
    try:
        available = set(tkfont.families())
    except Exception:
        return fallback
    for name in candidates:
        if name in available:
            return name
    return fallback


# ============================================================
#  3. 私钥处理
# ============================================================
def find_key_file():
    """
    按优先级查找私钥文件，返回路径；都找不到返回 None。

    ★ 打包成 --onefile 的 exe 后，每次运行都会把自身解压到临时目录，
      当前工作目录完全不可靠，写相对路径 100% 找不到文件。
    """
    if getattr(sys, "frozen", False):
        # 打包后：sys.executable 就是 exe 自身的完整路径
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    candidates = [
        os.path.join(base, KEY_NAME),                              # exe 同级
        os.path.join(base, "key", KEY_NAME),                       # exe/key/ 子目录
        os.path.join(os.path.expanduser("~"), ".ssh", KEY_NAME),   # 用户 .ssh
        KEY_FILE_HINT,                                             # 兜底绝对路径
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_private_key(path, passphrase=None):
    """尝试各种算法加载私钥；带密码时抛 PasswordRequiredException 交给上层弹窗"""
    errors = []
    for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_cls.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise                    # 需要密码，别被下面的 except 吞掉
        except paramiko.SSHException as exc:
            errors.append(f"{key_cls.__name__}: {exc}")
    raise paramiko.SSHException("无法识别该私钥格式：\n" + "\n".join(errors))


# ============================================================
#  4. SSH 长连接
# ============================================================
class SSHReader:
    """
    封装一条可复用的 SSH 长连接。

    对比：
      ✗ SSHClient.connect()      —— 每调用一次就重新 TCP + 握手 + 认证
      ✓ Transport + open_session —— 只在 connect() 里认证一次，
                                   之后 read_file() 复用同一条连接
    """

    def __init__(self, host, port, user, pkey):
        self.host = host
        self.port = port
        self.user = user
        self.pkey = pkey
        self.transport = None

    def connect(self):
        """建立 TCP + SSH 握手 + 密钥认证。★ 整个程序里只有这里会认证"""
        self.close()
        sock = socket.create_connection((self.host, self.port),
                                        timeout=CONNECT_TIMEOUT)
        # 建连后交回阻塞模式：超时统一由 Channel 层面控制，
        # 否则长连接在空闲时会被这个 socket 超时误伤
        sock.settimeout(None)

        transport = paramiko.Transport(sock)
        transport.set_keepalive(KEEPALIVE_SEC)      # 心跳，防被静默掐断
        transport.start_client(timeout=CONNECT_TIMEOUT)
        transport.auth_publickey(self.user, self.pkey)   # ★ 唯一一次认证
        self.transport = transport

    def is_connected(self):
        return self.transport is not None and self.transport.is_active()

    def read_file(self, path):
        """
        读取远程文件内容。复用已有连接，只开一个 channel，不产生新的认证。
        """
        if not self.is_connected():
            raise RuntimeError("SSH 连接不可用")

        chan = self.transport.open_session()
        try:
            chan.settimeout(CMD_TIMEOUT)
            chan.exec_command(f"cat {shlex.quote(path)}")

            # 手动收数据，比 makefile().read() 更可控，不会卡死
            chunks = []
            deadline = time.monotonic() + CMD_TIMEOUT
            while time.monotonic() < deadline:
                if chan.recv_ready():
                    chunks.append(chan.recv(65536))
                    continue
                if chan.exit_status_ready():
                    break
                time.sleep(0.02)
            else:
                raise TimeoutError(f"读取超时（{CMD_TIMEOUT} 秒）")

            while chan.recv_ready():
                chunks.append(chan.recv(65536))

            code = chan.recv_exit_status()
            if code != 0:
                err = b""
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                err_text = err.decode("utf-8", "replace").strip()
                low = err_text.lower()
                if "no such file" in low or "cannot open" in low:
                    raise FileNotFoundError(
                        f"服务器上找不到这个文件：\n{path}")
                raise RuntimeError(
                    f"读取失败（退出码 {code}）：{err_text or '无错误输出'}")
        finally:
            chan.close()

        return b"".join(chunks).decode("utf-8", "replace")

    def close(self):
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None


# ============================================================
#  5. 后台取数线程
# ============================================================
class NewsWorker(threading.Thread):
    """
    后台线程：负责 SSH 连接和定时读取。

    ★ 为什么必须放后台线程：SSH 读取是阻塞的，网络卡住时可能等十几秒。
      放在 tkinter 主线程里，窗口会在这段时间彻底卡死。
      tkinter 不是线程安全的，子线程只能往 queue 里塞数据，
      绝对不能直接改 Text 控件。
    """

    def __init__(self, ssh, path, interval, out_queue):
        super().__init__(daemon=True)
        self.ssh = ssh
        self.path = path
        self.interval = interval
        self.out_queue = out_queue
        self.wake = threading.Event()      # 用来"立刻刷新"或"立刻退出"
        self.stopped = threading.Event()

    def put(self, msg):
        self.out_queue.put(msg)

    def run(self):
        failures = 0
        while not self.stopped.is_set():
            delay = self.interval
            try:
                if not self.ssh.is_connected():
                    self.put(("status", "连接中", STATUS_WARN))
                    self.ssh.connect()              # ★ 只有这里会握手
                    self.put(("status", "已连接", STATUS_OK))

                content = self.ssh.read_file(self.path)
                self.put(("news", content, datetime.now()))
                failures = 0

            except FileNotFoundError as exc:
                # 文件不存在属于"配置问题"，不重复重连，按正常间隔重试
                failures = 0
                self.ssh.close()
                self.put(("error", str(exc)))
                delay = self.interval

            except Exception as exc:
                failures += 1
                self.ssh.close()
                self.put(("error", f"{type(exc).__name__}: {exc}"))
                # 连续失败 → 退避，避免疯狂重连
                delay = min(self.interval * (2 ** min(failures, 3)),
                            MAX_BACKOFF_SEC)
                self.put(("status", f"重试中 {int(delay)}s", STATUS_BAD))

            self.wake.wait(delay)
            self.wake.clear()

    def refresh_now(self):
        self.wake.set()

    def stop(self):
        self.stopped.set()
        self.wake.set()


# ============================================================
#  6. 悬浮窗（报纸风）
# ============================================================
class NewsApp:

    def __init__(self, root):
        self.root = root
        self.closing = False
        self.ssh = None
        self.worker = None
        self.q = queue.Queue()
        self._drag_x = 0
        self._drag_y = 0
        self._last_ok_at = None
        self._tick_job = None
        self._queue_job = None

        # ---- 置顶 / 沉底状态 ----
        self.pinned = True                     # True = 置顶；False = 已沉底
        self._hotkey_queue = queue.Queue()     # 热键线程 → 主线程 的事件队列
        self._hotkey_watcher = None
        self._hotkey_prev = False              # 兜底轮询用的边沿状态
        self._hotkey_armed_at = 0.0
        self._hotkey_job = None
        try:
            self._hotkey_vks = parse_hotkey(HOTKEY)
        except ValueError as exc:
            print(f"[警告] {exc}，全局快捷键已禁用")
            self._hotkey_vks = []

        # 字体在 Tk 根窗口建好之后才能探测
        serif = pick_serif_font(SERIF_CANDIDATES, SERIF_FALLBACK)
        self.f_body = (serif, 12)
        self.f_meta = (serif, 10)
        self.f_title = (serif, 16, "bold")
        self.f_status = (serif, 9)
        # ⬇ / 📌 是符号字形，用 Segoe UI Symbol 比中文字体更保险
        try:
            _fams = set(tkfont.families())
        except Exception:
            _fams = set()
        _sym = next((f for f in ("Segoe UI Symbol", "Segoe UI") if f in _fams),
                    serif)
        self.f_pin = (_sym, 11)

        self._build_window()
        self._build_widgets()
        self._place()
        self._boot()
        self._queue_job = self.root.after(200, self._poll_queue)
        self._start_hotkey()                   # 启动全局快捷键

    # ----------------------------------------------------------
    #  窗口
    # ----------------------------------------------------------
    def _build_window(self):
        root = self.root
        root.overrideredirect(True)         # ★ 无边框
        root.attributes("-topmost", True)   # ★ 始终置顶
        try:
            root.attributes("-alpha", ALPHA)  # 轻微透明 ≈ 磨砂感
        except tk.TclError:
            pass
        # 只给最外层一点淡描边，把纸面从桌面上"抬"起来
        root.configure(bg=BORDER)

        # 右键菜单（无边框窗口没有关闭按钮，退出必须靠它）
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="立即刷新", command=self._refresh_now)
        self.menu.add_command(label="⬇/📌  沉底 / 置顶", command=self.toggle_pin)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.on_quit)
        root.bind("<Button-3>", self._popup_menu)
        root.bind("<Escape>", lambda e: self.on_quit())

    # ----------------------------------------------------------
    #  界面
    # ----------------------------------------------------------
    def _build_widgets(self):
        # 外框（1px 淡边）
        outer = tk.Frame(self.root, bg=PAPER_BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        body = tk.Frame(outer, bg=PAPER_BG)
        body.pack(fill="both", expand=True, padx=14, pady=11)

        # ---------- 报头：标题（加粗衬线） + 状态 ----------
        head = tk.Frame(body, bg=PAPER_BG)
        head.pack(fill="x")

        self.lbl_title = tk.Label(head, text="每日财经", font=self.f_title,
                                  fg=INK_TITLE, bg=PAPER_BG, anchor="w")
        self.lbl_title.pack(side="left")

        # ★ 沉底 / 置顶按钮（报头最右侧，不参与拖动，避免想点却拖走窗口）
        self.pin_btn = tk.Button(head, text=self._pin_text(), font=self.f_pin,
                                 fg=INK, bg=PAPER_BG,
                                 activeforeground=INK_TITLE,
                                 activebackground="#e6e0d4",
                                 relief="flat", bd=0, highlightthickness=0,
                                 cursor="hand2", width=2,
                                 command=self.toggle_pin)
        self.pin_btn.pack(side="right", padx=(10, 0))
        self.pin_btn.bind("<Enter>", lambda e: self.pin_btn.config(fg=INK_TITLE))
        self.pin_btn.bind("<Leave>", lambda e: self._update_pin_btn())

        self.lbl_status = tk.Label(head, text="● 连接中", font=self.f_status,
                                   fg=STATUS_WARN, bg=PAPER_BG, anchor="e")
        self.lbl_status.pack(side="right")

        # ---------- 深色分割线（报纸的"通栏横线"）----------
        tk.Frame(body, bg=RULE, height=2).pack(fill="x", pady=(7, 9))

        # ---------- 正文：用固定像素尺寸的容器把 Text 卡成 420x250 ----------
        # tk.Text 的 width/height 是"字符数"，直接写 420 会得到一个巨大的窗口，
        # 所以套一层固定尺寸 + 关闭几何传播的 Frame，再让 Text 填满它。
        holder = tk.Frame(body, bg=TEXT_BG, width=TEXT_W, height=TEXT_H,
                          highlightthickness=0, bd=0)
        holder.pack(fill="x")
        holder.pack_propagate(False)        # ★ 关键：不让子控件撑大容器

        self.text = tk.Text(
            holder,
            wrap="word",                    # 按词换行，中文也适用
            font=self.f_body,
            fg=INK, bg=TEXT_BG,
            bd=0, relief="flat",
            highlightthickness=0,
            padx=2, pady=2,
            cursor="arrow",                 # 只读，不显示文本光标
            insertwidth=0,
            takefocus=0,                    # 不接受键盘焦点，不需要输入光标
            # ★ 彻底关掉"选中文字"：否则按住拖动会变成划选文字，窗口拖不动
            exportselection=False,          # 选中内容不写入系统剪贴板（也就没法复制）
            selectbackground=TEXT_BG,       # 万一是程序化选中，也让它看不见
            selectforeground=INK,
            spacing1=1, spacing2=1, spacing3=3,
        )
        self.text.pack(fill="both", expand=True)

        # 两段样式：正文 / 文末信息（小一号、偏灰，像报纸的"资料来源"）
        self.text.tag_configure("body", font=self.f_body, foreground=INK)
        self.text.tag_configure("meta", font=self.f_meta, foreground=INK_DIM,
                                spacing1=12)
        self.text.tag_configure("empty", font=self.f_body, foreground=INK_DIM,
                                justify="center", spacing1=80)

        self.text.insert("1.0", "正在连接服务器…", "empty")
        self.text.config(state="disabled")  # ★ 只读

        # 滚轮：显式绑定，保证 disabled 状态下也能滚
        self.text.bind("<MouseWheel>", self._on_wheel)        # Windows
        self.text.bind("<Button-4>", self._on_wheel)          # Linux 上滚
        self.text.bind("<Button-5>", self._on_wheel)          # Linux 下滚
        for seq in ("<Prior>", "<Next>", "<Up>", "<Down>"):
            self.text.bind(seq, lambda e: None)  # 不拦截，交给默认绑定

        # ★ 双击/三击默认会选中一个词/一行，这里一并吞掉，
        #   保证正文区任何位置按住都是"拖窗口"，不会再出现蓝色选区
        self.text.bind("<Double-Button-1>", lambda e: "break")
        self.text.bind("<Triple-Button-1>", lambda e: "break")
        # Ctrl+A / Ctrl+C 也没有意义了，直接吞掉，避免误触
        for seq in ("<Control-a>", "<Control-A>", "<Control-c>", "<Control-C>"):
            self.text.bind(seq, lambda e: "break")

        # ---------- 页脚：更新时间 + 提示 ----------
        foot = tk.Frame(body, bg=PAPER_BG)
        foot.pack(fill="x", pady=(8, 0))
        self.lbl_foot = tk.Label(foot, text="", font=self.f_meta,
                                 fg=INK_DIM, bg=PAPER_BG, anchor="w")
        self.lbl_foot.pack(side="left")

        # ---------- 拖动：绑定到所有非按钮部件 ----------
        for w in (self.root, outer, body, head, holder, self.text,
                  self.lbl_title, self.lbl_status, self.lbl_foot):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._drag)

    # ----------------------------------------------------------
    def _on_wheel(self, event):
        """只读 Text 的滚轮滚动（Windows 的 delta 是 120 的倍数）"""
        if getattr(event, "num", None) == 4:          # Linux 上滚
            step = -3
        elif getattr(event, "num", None) == 5:        # Linux 下滚
            step = 3
        else:
            step = -1 * int(event.delta / 120) * 3    # Windows
        self.text.yview_scroll(step, "units")
        return "break"

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y
        # ★ 在正文区必须吞掉事件（return "break"）：
        #   不吞的话 Tk 的 Text 类绑定会开始"选字"，
        #   于是"按住拖动"变成划选文字，窗口纹丝不动 —— 这就是拖不动的根因。
        if event.widget is self.text:
            return "break"

    def _drag(self, event):
        new_x = self.root.winfo_x() + event.x - self._drag_x
        new_y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{new_x}+{new_y}")
        if event.widget is self.text:
            return "break"

    def _place(self):
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = max(10, min(START_X, sw - w - 10))
        y = max(10, min(START_Y, sh - h - 10))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _refresh_now(self):
        if self.worker:
            self.lbl_status.config(text="● 刷新中", fg=STATUS_WARN)
            self.worker.refresh_now()

    # ----------------------------------------------------------
    #  启动：找私钥 → 加载 → 起线程
    # ----------------------------------------------------------
    def _boot(self):
        key_path = find_key_file()
        if not key_path:
            messagebox.showerror(
                "找不到私钥",
                f"没有找到私钥文件 {KEY_NAME}。\n\n"
                f"请把它放到下面任意一个位置（任选其一）：\n\n"
                f"  1) 程序同级目录\n"
                f"  2) 程序同级目录下的 key\\ 子目录\n"
                f"  3) {os.path.join(os.path.expanduser('~'), '.ssh')}\n\n"
                f"注意：私钥【不要】打包进 exe。")
            self.on_quit()
            return

        try:
            pkey = load_private_key(key_path)
        except paramiko.PasswordRequiredException:
            pw = simpledialog.askstring(
                "私钥密码",
                f"私钥有密码保护：\n{key_path}\n\n请输入密码：", show="*")
            if not pw:
                messagebox.showwarning("已取消", "没有输入私钥密码，程序退出。")
                self.on_quit()
                return
            try:
                pkey = load_private_key(key_path, pw)
            except Exception as exc:
                messagebox.showerror("私钥加载失败",
                                     f"{type(exc).__name__}: {exc}")
                self.on_quit()
                return
        except Exception as exc:
            messagebox.showerror("私钥加载失败",
                                 f"{type(exc).__name__}: {exc}\n\n{key_path}")
            self.on_quit()
            return

        self.ssh = SSHReader(HOST, PORT, USER, pkey)
        self.worker = NewsWorker(self.ssh, NEWS_PATH, REFRESH_SEC, self.q)
        self.worker.start()

    # ----------------------------------------------------------
    #  从队列取消息更新界面（只在主线程执行）
    # ----------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]

                if kind == "news":
                    _, content, when = msg
                    self._render(content, when)
                    self.lbl_status.config(text="● 已连接", fg=STATUS_OK)
                    self._last_ok_at = when

                elif kind == "status":
                    self.lbl_status.config(text=f"● {msg[1]}", fg=msg[2])

                elif kind == "error":
                    self._show_error(msg[1])

        except queue.Empty:
            pass

        if not self.closing:
            self._queue_job = self.root.after(200, self._poll_queue)

    def _show_error(self, text):
        """
        出错时把原因写进正文，但【不清空】已经读到的内容 ——
        网络抖一下就白屏是最让人恼火的体验。
        """
        self.lbl_status.config(text="● 读取失败", fg=STATUS_BAD)
        current = self.text.get("1.0", "end-1c").strip()
        if not current or current.startswith("正在连接"):
            self._set_text(text, tag="body")

    def _split_meta(self, content):
        """
        把正文和文末的"资料来源/生成时间"分开：
        用一行 3 个以上 '-' 作为分隔（daily_news.py 生成的就是这个格式）。
        分开后文末用小一号灰字，更像报纸的资料来源栏。
        """
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.strip() and set(line.strip()) == {"-"} and len(line.strip()) >= 3:
                return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i + 1:]).strip()
        return content.rstrip(), ""

    def _set_text(self, content, tag="body"):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content, tag)
        self.text.config(state="disabled")

    def _render(self, content, when):
        content = (content or "").strip()
        if not content:
            self._set_text("服务器上的 \n%s\n还是空的。\n\n"
                           "先去服务器把文件写好，再右键「立即刷新」。"
                           % NEWS_PATH, tag="empty")
            self.lbl_foot.config(text=f"{when:%H:%M} 读取成功 · 文件为空")
            return

        body, meta = self._split_meta(content)

        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", body, "body")
        if meta:
            self.text.insert("end", "\n" + meta, "meta")
        self.text.config(state="disabled")
        self.text.yview_moveto(0)          # 新内容从头看

        n = len(content)
        self.lbl_foot.config(
            text=f"更新于 {when:%H:%M:%S} · {n} 字 · 每 {REFRESH_SEC // 60} 分钟自动刷新")

    # ----------------------------------------------------------
    #  置顶 / 沉底
    # ----------------------------------------------------------
    def _pin_text(self):
        """按钮图标：置顶时显示 ⬇（点它沉底），沉底时显示 📌（点它置顶）"""
        return PIN_TEXT_UP if self.pinned else PIN_TEXT_DOWN

    def _update_pin_btn(self):
        try:
            self.pin_btn.config(text=self._pin_text(),
                                fg=INK if self.pinned else INK_DIM)
        except Exception:
            pass

    def toggle_pin(self):
        """按钮和全局快捷键共用的开关"""
        if self.pinned:
            self.sink_to_bottom()
        else:
            self.raise_to_top()

    def sink_to_bottom(self):
        """
        沉到所有窗口的底层。

        顺序很重要：★ 必须先在 Tk 层取消 -topmost，再用 HWND_BOTTOM。
        因为 topmost 是"置顶层"的属性，不取消的话窗口仍留在置顶层里，
        HWND_BOTTOM 只能把它排到置顶层的最底 —— 依旧盖着普通窗口。
        """
        self.pinned = False
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass
        if IS_WINDOWS:
            hwnd = get_hwnd(self.root)
            if not win_set_pos(hwnd, _HWND_BOTTOM,
                               _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE):
                # 极少数情况会失败，去掉 NOACTIVATE 再试一次
                win_set_pos(hwnd, _HWND_BOTTOM, _SWP_NOMOVE | _SWP_NOSIZE)
        self._update_pin_btn()

    def raise_to_top(self):
        """重新置顶并显示到最前面（全局快捷键唤醒走这里）"""
        self.pinned = True
        try:
            self.root.deiconify()               # 万一被隐藏过，先恢复显示
        except Exception:
            pass
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        if IS_WINDOWS:
            hwnd = get_hwnd(self.root)
            win_set_pos(hwnd, _HWND_TOPMOST,
                        _SWP_NOMOVE | _SWP_NOSIZE
                        | _SWP_SHOWWINDOW | _SWP_NOACTIVATE)
        try:
            self.root.lift()                    # 双保险
        except Exception:
            pass
        self._update_pin_btn()

    def _start_hotkey(self):
        """启动全局快捷键：优先 RegisterHotKey，注册失败才退回轮询"""
        if not IS_WINDOWS or not self._hotkey_vks:
            return
        self._hotkey_watcher = HotkeyWatcher(HOTKEY, self._hotkey_queue)
        self._hotkey_watcher.start()
        # 给注册线程一点时间，过了宽限期再判断是否要轮询兜底
        self._hotkey_armed_at = time.monotonic() + 0.4
        self._hotkey_job = self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)

    def _poll_hotkey(self):
        """
        主路径：RegisterHotKey 线程收到按键就往队列里放，这里取出来切换。
                （窗口沉底、没有焦点时照样有效，这是唤醒窗口的唯一手段）
        兜底：万一注册失败（键被别的程序占用），退回按键轮询。
        """
        if self.closing:
            return
        try:
            # 1) 主路径：处理系统投递过来的热键事件
            while True:
                try:
                    self._hotkey_queue.get_nowait()
                except queue.Empty:
                    break
                self.toggle_pin()

            # 2) 兜底轮询：仅在注册失败、且已过宽限期时启用
            w = self._hotkey_watcher
            if (w is not None and not w.ok
                    and time.monotonic() >= self._hotkey_armed_at
                    and self._hotkey_vks):
                states = [key_state(vk) for vk in self._hotkey_vks]
                held = all(s & 0x8000 for s in states)
                if held and not self._hotkey_prev:
                    self.toggle_pin()
                self._hotkey_prev = held
        except Exception:
            pass
        try:
            self._hotkey_job = self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)
        except Exception:
            pass                                # 窗口已销毁

    # ----------------------------------------------------------
    #  退出
    # ----------------------------------------------------------
    def on_quit(self):
        if self.closing:
            return
        self.closing = True
        try:
            if self._hotkey_watcher is not None:
                self._hotkey_watcher.stop()    # 注销热键，别一直占着键
        except Exception:
            pass
        try:
            if self.worker:
                self.worker.stop()
                self.worker.join(timeout=2)
        except Exception:
            pass
        try:
            if self.ssh:
                self.ssh.close()
        except Exception:
            pass
        for job in (self._queue_job, self._tick_job, self._hotkey_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._queue_job = self._tick_job = self._hotkey_job = None
        try:
            self.root.destroy()
        except Exception:
            pass


# ============================================================
#  7. 入口
# ============================================================
def enable_dpi_awareness():
    """让窗口在高分屏上保持清晰。失败也不影响运行。"""
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main():
    enable_dpi_awareness()
    root = tk.Tk()

    cfg = load_config()
    if not config_complete(cfg):
        root.withdraw()
        new_cfg = run_config_wizard(root, cfg)
        if new_cfg is None:
            root.destroy()
            print("未完成配置，已退出。")
            return
        save_config(new_cfg)
        cfg = new_cfg
        print("配置已保存到：%s" % config_path())
    apply_config(cfg)

    root.deiconify()
    NewsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()