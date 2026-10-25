# -*- coding: utf-8 -*-
"""
ncmd.py —— 命令行网易云音乐搜索 / 试听 / 下载工具

流程：
  1. 扫码登录网页版网易云音乐（终端直接显示二维码）
  2. 命令行输入歌名 -> 自动搜索 -> 逐行列出结果
  3. 输入序号 -> 自动播放该歌曲（ffplay 流式播放）
  4. 确认后自动解析并下载「音乐文件 + 歌词文件」到配置目录（默认 D:\\music）

解析与下载走 wyapi.toubiec.cn 的同源接口：
  POST https://nextmusic.toubiec.cn/api/ip            获取客户端 IP（接口要求）
  POST https://nextmusic.toubiec.cn/api/getSongUrl    取音频直链 {id, level, timestamp, ip}
  POST https://nextmusic.toubiec.cn/api/getSongLyric  取歌词      {id, timestamp, ip}
  level 取值：standard / exhigh / lossless / hires / jyeffect / sky / dolby / vivid / jymaster
  （即页面上「标准音质 / 极高音质 / 无损音质 / Hi-Res音质 / 高清环绕声 / 沉浸环绕声 /
     杜比全景声 / 臻音全景声 / 超清母带」九档）

依赖：仅 Python 标准库（二维码为自研实现，见 pyqr.py）+ 可选的 ffplay/ffmpeg。
"""

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pyqr          # noqa: E402
import lrcutil       # noqa: E402

# ----------------------------------------------------------------- 基础设置
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
COOKIE_PATH = APP_DIR / "cookie.txt"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "download_dir": r"D:\music",
    "quality": "无损音质",
    "quality_fallback": ["无损音质", "极高音质", "标准音质"],
    "search_limit": 20,
    "filename_format": "song-singer",
    "include_translation": True,
    "write_srt": False,
    "tag_metadata": True,
    "skip_existing": True,
    "open_player_on_select": True,
    "login_on_start": True,
    "login_method": "ask",
    "playback_debug": False,
    "player": "ffplay",
    "request_gap_ms": 1200,
    "api_base": "https://nextmusic.toubiec.cn",
}

QUALITY_LABELS = [
    ("standard", "标准音质"),
    ("exhigh", "极高音质"),
    ("lossless", "无损音质"),
    ("hires", "Hi-Res音质"),
    ("jyeffect", "高清环绕声"),
    ("sky", "沉浸环绕声"),
    ("dolby", "杜比全景声"),
    ("vivid", "臻音全景声"),
    ("jymaster", "超清母带"),
]
QUALITY_BY_LABEL = {label: code for code, label in QUALITY_LABELS}
QUALITY_LABEL_BY_CODE = {code: label for code, label in QUALITY_LABELS}
LEVEL_ORDER = [code for code, _ in QUALITY_LABELS]

CTX = ssl.create_default_context()

# ----------------------------------------------------------------- 输出工具
def enable_utf8():
    """让 Windows 控制台支持中文与制表符。"""
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def out(*args):
    print(*args, flush=True)


def rule(char="-", n=74):
    out(char * n)


def human_size(n):
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def human_dur(ms):
    if not ms:
        return "--:--"
    s = int(ms) // 1000
    return "%d:%02d" % (s // 60, s % 60)


def terminal_width(default=100):
    try:
        return shutil.get_terminal_size((default, 30)).columns
    except Exception:
        return default


def disp_width(text):
    """近似显示宽度（中日韩字符按 2 列计）。"""
    w = 0
    for ch in text:
        w += 2 if ord(ch) > 0x1100 and (
            0x2E80 <= ord(ch) <= 0xA4CF or 0xAC00 <= ord(ch) <= 0xD7A3
            or 0xF900 <= ord(ch) <= 0xFAFF or 0xFE30 <= ord(ch) <= 0xFE6F
            or 0xFF00 <= ord(ch) <= 0xFF60 or 0xFFE0 <= ord(ch) <= 0xFFE6
            or 0x20000 <= ord(ch) <= 0x3FFFD) else 1
    return w


def pad(text, width):
    text = str(text)
    return text + " " * max(0, width - disp_width(text))


def truncate(text, width):
    text = str(text)
    if disp_width(text) <= width:
        return text
    buf, w = "", 0
    for ch in text:
        cw = disp_width(ch)
        if w + cw > width - 1:
            break
        buf += ch
        w += cw
    return buf + "…"


# ----------------------------------------------------------------- 配置
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
        except Exception as e:
            out("[!] config.json 解析失败，使用默认配置：%s" % e)
    else:
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            out("[i] 已生成默认配置文件：%s" % CONFIG_PATH)
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        out("[!] 保存配置失败：%s" % e)


def resolve_quality(cfg):
    """把配置里的音质名称解析成 level 代码，并返回尝试顺序。"""
    if isinstance(cfg.get("quality_fallback"), list) and cfg["quality_fallback"]:
        order = list(cfg["quality_fallback"])
    else:
        order = [cfg.get("quality", "lossless"), "exhigh", "standard"]
    codes = []
    for item in order:
        if not item:
            continue
        code = item if item in QUALITY_LABEL_BY_CODE else QUALITY_BY_LABEL.get(item)
        if code and code not in codes:
            codes.append(code)
    return codes or ["lossless", "exhigh", "standard"]


# ----------------------------------------------------------------- HTTP
def load_device_id():
    """持久化一个 deviceId，让请求更像真实浏览器（有助于降低风控概率）。"""
    path = APP_DIR / "device_id.txt"
    try:
        if path.exists():
            did = path.read_text(encoding="utf-8").strip()
            if len(did) >= 16:
                return did
    except Exception:
        pass
    did = uuid.uuid4().hex[:32].upper()
    try:
        path.write_text(did, encoding="utf-8")
    except Exception:
        pass
    return did


DEVICE_ID = None


def seed_device_cookies(jar, device_id=None):
    """写入 deviceId / os / appver 等 cookie（网易云网页端本来就会带）。"""
    global DEVICE_ID
    if DEVICE_ID is None:
        DEVICE_ID = load_device_id()
    did = device_id or DEVICE_ID
    pairs = [
        ("deviceId", did),
        ("os", "pc"),
        ("appver", "8.9.70"),
        ("osver", "Microsoft-Windows-10-Professional-build-19045-64bit"),
        ("channel", "netease"),
    ]
    for name, value in pairs:
        try:
            jar.set_cookie(http.cookiejar.Cookie(
                0, name, value, None, False, ".music.163.com", False, False, "/",
                True, False, None, False, None, None, {}))
        except Exception:
            pass


def build_opener(cookie_path=None):
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path)) if cookie_path else http.cookiejar.CookieJar()
    if cookie_path and Path(cookie_path).exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    seed_device_cookies(jar)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA)]
    return opener, jar


def http_json(opener, url, payload, timeout=30, referer=None):
    """POST JSON，返回解析后的 dict。"""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def http_post_form(opener, url, payload, timeout=25, referer="https://music.163.com/"):
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Referer": referer, "User-Agent": UA}
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip().startswith(("{", "[")) else {}


def http_get_bytes(opener, url, timeout=60, extra_headers=None):
    headers = {"User-Agent": UA}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers), resp.status


# ----------------------------------------------------------------- 登录
def qr_login(opener, jar, cookie_path, timeout_s=180):
    """终端显示二维码并轮询登录状态；成功返回用户昵称。"""
    out("\n[1/4] 正在向网易云音乐申请登录二维码 ...")
    data = http_post_form(opener, "https://music.163.com/api/login/qrcode/unikey", {"type": 1})
    unikey = data.get("unikey")
    if not unikey:
        raise RuntimeError("获取二维码 key 失败：%s" % data)

    login_url = "https://music.163.com/login?codekey=%s" % unikey
    out("      二维码链接：%s" % login_url)
    out("      请用「网易云音乐」App 扫描下方二维码并在手机上确认登录：\n")

    lines = pyqr.qr_lines(login_url, ecc="M", quiet=2, half_block=True)
    width = terminal_width()
    if len(lines[0]) > width - 2:
        lines = pyqr.qr_lines(login_url, ecc="L", quiet=1, half_block=True)
    if len(lines[0]) > width - 2:
        out("      （当前终端过窄，二维码显示可能不完整：请把窗口拉宽到 %d 列以上）\n"
            % (len(lines[0]) + 2))
    for line in lines:
        out("      " + line)
    out("")

    deadline = time.time() + timeout_s
    scanned_notified = False
    while time.time() < deadline:
        try:
            st = http_post_form(opener, "https://music.163.com/api/login/qrcode/client/login",
                                {"key": unikey, "type": 1})
        except urllib.error.HTTPError as e:
            st = {"code": e.code}
        code = st.get("code")
        if code == 800:
            raise RuntimeError("二维码已过期，请重新运行程序。")
        if code == 801:
            pass                                   # 等待扫码
        elif code == 802:
            if not scanned_notified:
                out("      √ 已扫码，请在手机上点击「确认登录」...")
                scanned_notified = True
        elif code == 803:
            try:
                jar.save(str(cookie_path), ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
            profile = st.get("profile") or {}
            nick = profile.get("nickname") or profile.get("userId") or "未知用户"
            out("      √ 登录成功：%s（Cookie 已保存到 %s）" % (nick, cookie_path.name))
            return nick
        elif code == 8821:
            # 网易风控：常出现在"扫码环境异常/版本过低"时
            raise RuntimeError("网易风控拦截（8821）：%s\n"
                               "      该限制来自网易风控，通常与当前网络环境/客户端版本有关，"
                               "与程序无关。\n"
                               "      建议改用手机号登录（选择 1）。"
                               % (st.get("message") or "请切换其他登录方式或升级新版本再试"))
        else:
            msg = st.get("message") or ""
            if st.get("redirectUrl") and "anquanhuanjingfengxian" in str(st.get("redirectUrl")):
                raise RuntimeError("网易风控拦截（%s）：%s\n"
                                   "      建议改用手机号登录（选择 1）。" % (code, msg))
            out("      [!] 未预期的返回：%s" % st)
        time.sleep(2.5)
    raise RuntimeError("等待扫码超时（%d 秒）。" % timeout_s)


def send_sms_code(opener, phone, country="86"):
    """发送短信验证码，返回 (ok, 提示信息)。"""
    data = {"cellphone": phone, "countrycode": country, "ctcode_%s" % country: ""}
    try:
        d = http_post_form(opener, "https://music.163.com/api/sms/captcha/sent", data)
    except Exception as e:
        return False, "请求发送验证码失败：%s" % e
    code = d.get("code")
    if code == 200:
        return True, "验证码已发送"
    if code == 405:
        return False, "发送过于频繁，请稍等一会儿再试（%s）" % d.get("message", "")
    if code == 406:
        return False, "操作频繁，请稍候再试（网易限制同一号码的发送频率）"
    if code == 400:
        return False, "手机号格式不正确，请检查后重试"
    if code == 8821:
        return False, "网易风控拦截：%s" % d.get("message", "")
    return False, d.get("message") or d.get("msg") or ("发送失败（code=%s）" % code)


def phone_login(opener, jar, cookie_path):
    """手机号登录：支持 短信验证码 与 密码 两种方式。"""
    out("\n[1/4] 手机号登录")
    country = ask("      国家/地区码（中国大陆直接回车，默认 86）> ") or "86"
    country = re.sub(r"\D", "", country) or "86"
    phone = re.sub(r"\D", "", ask("      手机号 > "))
    if len(phone) < 6:
        raise RuntimeError("手机号看起来不正确。")

    out("      登录方式：1 = 短信验证码（推荐）  2 = 手机号 + 密码")
    mode = (ask("      请选择（默认 1）> ") or "1").strip()
    if mode not in ("1", "2"):
        mode = "1"

    payload = {"phone": phone, "countrycode": country, "rememberLogin": "true"}

    if mode == "1":
        ok, msg = send_sms_code(opener, phone, country)
        out("      %s %s" % ("√" if ok else "[!]", msg))
        if not ok:
            out("      · 若一直失败，可改用密码登录（下次选择 2），或稍后重试。")
        code = re.sub(r"\D", "", ask("      请输入收到的短信验证码 > "))
        if not code:
            raise RuntimeError("没有输入验证码。")
        payload["captcha"] = code
    else:
        import getpass
        try:
            pwd = getpass.getpass("      请输入密码（输入时不显示）> ")
        except Exception:
            pwd = ask("      请输入密码 > ")
        if not pwd:
            raise RuntimeError("没有输入密码。")
        payload["password"] = pwd

    out("      正在登录 ...")
    try:
        d = http_post_form(opener, "https://music.163.com/api/w/login/cellphone", payload)
    except Exception as e:
        raise RuntimeError("登录请求失败：%s" % e)

    code = d.get("code")
    if code == 200:
        profile = d.get("profile") or {}
        nick = profile.get("nickname") or profile.get("userId") or "未知用户"
        try:
            jar.save(str(cookie_path), ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
        out("      √ 登录成功：%s（Cookie 已保存到 %s）" % (nick, cookie_path.name))
        return nick

    hints = {
        501: "账号或密码错误",
        502: "账号不存在或密码错误",
        503: "验证码错误或已过期，请重新获取",
        504: "该账号需要额外验证",
        400: "参数有误（请检查手机号与区号）",
        8821: "网易风控拦截，请稍后重试或改在网络环境更干净的地方登录",
    }
    raise RuntimeError("登录失败（code=%s）：%s"
                       % (code, d.get("message") or d.get("msg") or hints.get(code, "未知错误")))


def choose_login_method(cfg, has_cookie=False):
    """未指定登录方式时，让用户选择：1 手机号 / 2 二维码。"""
    preferred = str(cfg.get("login_method", "ask")).strip().lower()
    alias = {"phone": "1", "cellphone": "1", "mobile": "1", "sms": "1",
             "qr": "2", "qrcode": "2", "scan": "2"}
    preferred = alias.get(preferred, preferred)

    out("")
    rule("=")
    out(" 请选择登录方式")
    rule("=")
    if has_cookie:
        out("  0. 使用本地已保存的登录状态（cookie.txt）")
    out("  1. 手机号登录（短信验证码 / 密码）")
    out("  2. 二维码扫码登录（用网易云音乐 App 扫）")
    rule("=")

    if preferred in ("1", "2"):
        out("[i] 已按 config.json 的 login_method = %s 自动选择" % preferred)
        return preferred

    while True:
        ans = (ask("输入 1 或 2%s > " % ("（或 0 复用本地登录）" if has_cookie else "")) or "").strip()
        if ans in ("1", "2"):
            return ans
        if ans == "0" and has_cookie:
            return "0"
        if not ans:
            out("  [!] 请输入 1 或 2。")
        else:
            out("  [!] 无效输入，请输入 1 或 2。")


def check_login(opener):
    """返回已登录用户昵称，未登录返回 None。"""
    try:
        data = http_post_form(opener, "https://music.163.com/api/nuser/account/get", {})
        profile = data.get("profile")
        if profile:
            return profile.get("nickname") or str(profile.get("userId"))
        cookie = data.get("account")
        if cookie:
            return "已登录账号"
    except Exception:
        pass
    return None


def login_by_method(cfg, opener, jar, cookie_path, method):
    """按指定方式登录：'1' = 手机号，'2' = 二维码。"""
    if method == "1":
        return phone_login(opener, jar, cookie_path)
    return qr_login(opener, jar, cookie_path)


def ensure_login(opener, jar, cookie_path, force=False, required=False, cfg=None):
    """确保登录态。

    - 有本地 Cookie 且未强制重新登录：直接复用
    - 需要登录时按 --login / config.login_method / 交互选择 决定方式
    - 二维码遇到网易风控(8821) 时，自动引导改用手机号登录
    - required=False 时最终失败也不中断，改用匿名模式继续
    """
    cfg = cfg or {}
    has_cookie = bool(cookie_path and Path(cookie_path).exists())
    if not force:
        nick = check_login(opener)
        if nick:
            out("[1/4] 已使用本地 Cookie 登录：%s" % nick)
            return nick

    try:
        method = choose_login_method(cfg, has_cookie=has_cookie)
        if method == "0":
            nick = check_login(opener)
            if nick:
                out("[1/4] 已复用本地登录状态：%s" % nick)
                return nick
            out("[!] 本地登录状态已失效，请重新选择登录方式。")
            method = choose_login_method({}, has_cookie=False)
        try:
            return login_by_method(cfg, opener, jar, cookie_path, method)
        except Exception as e:
            # 二维码被风控拦截时，主动提示并直接切到手机号登录
            if method == "2" and "8821" in str(e):
                out("\n[!] %s" % e)
                ans = (ask("      是否现在改用手机号登录？（y/n，默认 y）> ") or "y").lower()
                if ans in ("y", "yes", "是", "1", ""):
                    return phone_login(opener, jar, cookie_path)
            raise
    except KeyboardInterrupt:
        raise
    except Exception as e:
        if required:
            raise
        out("[!] 登录未完成：%s" % e)
        out("    将以匿名模式继续：搜索、试听、下载都不依赖登录态；")
        out("    需要登录时重新运行并加 --relogin 即可。")
        return None


# ----------------------------------------------------------------- 搜索
def search_songs(opener, keyword, limit=20, offset=0):
    data = http_post_form(opener, "https://music.163.com/api/search/get",
                          {"s": keyword, "type": 1, "offset": offset, "limit": limit})
    result = data.get("result") or {}
    songs = []
    for s in result.get("songs") or []:
        songs.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "artists": "/".join(a.get("name", "") for a in (s.get("artists") or [])),
            "album": (s.get("album") or {}).get("name", ""),
            "duration": s.get("duration") or 0,
            "fee": s.get("fee", 0),
        })
    return songs, result.get("songCount", len(songs))


def print_songs(songs, page=1, limit=20):
    out("")
    rule("=")
    out(" %-4s %-34s %-20s %8s  %s" % ("序号", "歌曲", "歌手", "时长", "专辑"))
    rule("=")
    for i, s in enumerate(songs, 1):
        mark = "*" if s.get("fee") == 1 else " "
        out(" %s%-3d %-34s %-20s %8s  %s"
            % (mark, i, truncate(s["name"], 33), truncate(s["artists"], 19),
               human_dur(s["duration"]), truncate(s["album"], 22)))
    rule("=")
    out(" 提示：* = VIP/付费歌曲，可能取不到直链")
    out(" " + HELP_TEXT)


# ----------------------------------------------------------------- 解析接口
class WyyApi:
    """wyapi.toubiec.cn 页面所用的同源解析接口。"""

    def __init__(self, cfg, opener):
        self.base = (cfg.get("api_base") or DEFAULT_CONFIG["api_base"]).rstrip("/")
        self.opener = opener
        self._ip = None
        self._last = 0.0
        self.gap = max(0, int(cfg.get("request_gap_ms", 1200))) / 1000.0

    def client_ip(self):
        if self._ip:
            return self._ip
        try:
            d = http_json(self.opener, self.base + "/api/ip",
                          {"timestamp": int(time.time() * 1000)}, referer="https://wyapi.toubiec.cn/")
            self._ip = ((d.get("data") or {}).get("ip") or "").strip()
        except Exception:
            self._ip = ""
        return self._ip

    def _call(self, path, payload, retry_429=True, timeout=30):
        payload = dict(payload)
        payload["timestamp"] = int(time.time() * 1000)
        ip = self.client_ip()
        if ip:
            payload["ip"] = ip
        # 控制请求频率，避免触发 429
        wait = self.gap - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            d = http_json(self.opener, self.base + path, payload, timeout=timeout,
                          referer="https://wyapi.toubiec.cn/")
            self._last = time.time()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                d = json.loads(body)
            except Exception:
                d = {"code": e.code, "message": body[:200]}
            self._last = time.time()
        code = d.get("code")
        if code == 429:
            msg = d.get("message") or "请求过于频繁"
            m = re.search(r"(\d+)\s*秒", msg)
            need = int(m.group(1)) + 2 if m else 32
            if retry_429:
                out("      [!] %s，等待 %d 秒后重试..." % (msg, need))
                time.sleep(need)
                return self._call(path, payload, retry_429=False, timeout=timeout)
            raise RuntimeError(msg)
        return d

    def song_info(self, sid):
        return (self._call("/api/getSongInfo", {"id": int(sid)}).get("data") or {})

    def song_url(self, sid, level):
        return self._call("/api/getSongUrl", {"id": int(sid), "level": level})

    def lyric(self, sid):
        return (self._call("/api/getSongLyric", {"id": int(sid)}).get("data") or {})

    def resolve_url(self, sid, levels):
        """按音质顺序尝试，返回 (url, info dict, 失败原因列表)。"""
        errors = []
        for lv in levels:
            label = QUALITY_LABEL_BY_CODE.get(lv, lv)
            try:
                d = self.song_url(sid, lv)
            except Exception as e:
                errors.append("%s：%s" % (label, e))
                continue
            data = d.get("data") or {}
            url = data.get("url")
            if url:
                return url, data, errors
            msg = d.get("message") or ("该音质暂无直链" if d.get("code") == 200 else "接口返回异常")
            errors.append("%s：%s" % (label, msg))
        return None, {}, errors


# ----------------------------------------------------------------- 下载
def detect_ext(path):
    """按文件头判断真实格式。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return ".mp3"
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ".mp3"
    if head[4:8] in (b"ftyp", b"moov", b"mdat"):
        return ".m4a"
    if head[:4] == b"OggS":
        return ".ogg"
    return ".mp3"


def download_file(opener, url, dest_stem, expected_size=0, chunk=262144):
    """支持断点续传的下载；返回最终文件路径。"""
    tmp = Path(str(dest_stem) + ".part")
    dest_dir = tmp.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    have = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Accept": "*/*"}
    if have:
        headers["Range"] = "bytes=%d-" % have
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **headers))
    try:
        resp = opener.open(req, timeout=90)
    except urllib.error.HTTPError as e:
        if have and e.code == 416:      # 本地 .part 已完整，直接重下
            resp = opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=90)
            have = 0
        else:
            raise

    with resp:
        total = expected_size or 0
        cr = resp.headers.get("Content-Range")
        if cr and "/" in cr:
            try:
                total = int(cr.split("/")[-1])
            except ValueError:
                pass
        elif resp.headers.get("Content-Length"):
            total = int(resp.headers["Content-Length"]) + have
        if resp.status == 200 and have:
            have = 0            # 服务端不支持断点，重新写
        mode = "ab" if have else "wb"
        got = have
        last_pct = -1
        with open(tmp, mode) as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                if total:
                    pct = int(got * 100 / total)
                    if pct != last_pct and pct % 5 == 0:
                        out("      下载中 %3d%%  (%s / %s)" % (pct, human_size(got), human_size(total)))
                        last_pct = pct
    final = Path(str(dest_stem) + detect_ext(tmp))
    if final.exists():
        final.unlink()
    tmp.replace(final)
    return final, got


AUDIO_TAG_MAP = {".mp3": "mp3", ".flac": "flac", ".m4a": "m4a"}


def tag_audio(cfg, path, name, artist, album, lyric_text=None):
    """用 ffmpeg 写入标题/歌手/专辑（无损复制，不改音频数据）。"""
    if not cfg.get("tag_metadata", True):
        return True
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        return False
    ext = Path(path).suffix.lower()
    container = AUDIO_TAG_MAP.get(ext)
    if not container:
        return False
    out_path = Path(str(path) + ".tagged" + ext)
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-c", "copy", "-metadata", "title=%s" % name, "-metadata", "artist=%s" % artist,
            "-metadata", "album=%s" % album]
    args.append(str(out_path))
    try:
        r = subprocess.run(args, capture_output=True, timeout=120)
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            Path(path).unlink()
            out_path.replace(path)
            return True
    except Exception:
        pass
    try:
        if out_path.exists():
            out_path.unlink()
    except Exception:
        pass
    return False


def build_filename(cfg, name, artist, ext_hint=""):
    fmt = cfg.get("filename_format", "song-singer")
    if fmt == "song":
        stem = name
    elif fmt == "singer-song":
        stem = "%s - %s" % (artist, name)
    else:
        stem = "%s - %s" % (name, artist)
    stem = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", stem).strip() or "未命名"
    return stem[:120]


# ----------------------------------------------------------------- 播放
def find_player(cfg):
    cand = cfg.get("player") or "ffplay"
    p = shutil.which(cand)
    if p:
        return p
    if cand.lower().startswith("ffplay"):
        for guess in (r"C:\Program Files\ffmpeg\bin\ffplay.exe",
                      r"C:\ffmpeg\bin\ffplay.exe"):
            if Path(guess).exists():
                return guess
    return None


def _playback_debug(msg):
    """ncmd-music\\playback_debug.log —— 排查按键问题时打开（config.json: playback_debug）。"""
    try:
        with open(APP_DIR / "playback_debug.log", "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _read_key():
    """非阻塞读取一个按键：返回 (是否有按键, 字符, 是否为功能键)。

    注意：Ctrl+C 会被 getwch() 以 '\\x03' 读出，这里重新抛成 KeyboardInterrupt，
    避免吃掉用户的中断信号。
    """
    try:
        import msvcrt
    except Exception:
        return False, "", False
    try:
        if not msvcrt.kbhit():
            return False, "", False
        ch = msvcrt.getwch()
    except Exception:
        return False, "", False
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in ("\x00", "\xe0"):          # 功能键/方向键的前导字节
        try:
            second = msvcrt.getwch()
        except Exception:
            second = ""
        return True, second, True
    return True, ch, False


def _make_ffplay_stdin(proc):
    """把 ffplay 的 stdin 接到一个空管道。

    目的：-nodisp 模式下 ffplay 没有窗口，却仍会去读 stdin。若让它继承控制台，
    它就会把按键（空格/方向键等）从控制台输入缓冲里抢走，Python 这边再也读不到。
    接上管道后，所有按键都归程序处理，暂停/停止由我们自己实现。
    """
    try:
        r, w = os.pipe()
        os.set_handle_inheritable(w, True)      # 写端给子进程继承
        os.close(r)                             # 我们只写不读
        os.set_blocking(w, False)
        proc.stdin = os.fdopen(w, "wb", buffering=0)
    except Exception:
        proc.stdin = None


def _spawn_ffplay(cfg, url, start_sec, title=""):
    player = find_player(cfg)
    if not player:
        return None
    args = [player, "-nodisp", "-autoexit", "-loglevel", "warning"]
    if start_sec and start_sec > 0.5:
        args += ["-ss", "%.2f" % start_sec]     # 从暂停位置继续
    args += ["-i", url]
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE)
    except Exception as e:
        out("      [!] 播放失败：%s" % e)
        return None
    _make_ffplay_stdin(proc)
    return proc


def _drain_keys(debug=False):
    """丢弃缓冲区里遗留的按键（播放前调用，避免误触）。"""
    try:
        import msvcrt
    except Exception:
        return
    try:
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if debug:
                _playback_debug("drained %r" % ch)
            if ch in ("\x00", "\xe0") and msvcrt.kbhit():
                msvcrt.getwch()
    except Exception:
        pass


def play_stream(cfg, url, title=""):
    """流式播放，播放中可随时按键控制。

    快捷键：q / Q / Esc = 停止，空格 = 暂停/继续，Ctrl+C = 停止

    实现要点：ffplay 的 stdin 接空管道（不抢按键），按键由本进程独占读取；
    暂停用「记住播放位置 + 结束 ffplay + 按位置重启」实现，因此不依赖 ffplay 的交互模式。
    """
    if not find_player(cfg):
        out("      [!] 未找到播放器 ffplay（可安装 ffmpeg，或在 config.json 里设置 player 路径）。")
        return False

    debug = bool(cfg.get("playback_debug"))
    if debug:
        _playback_debug("--- play_stream start: %s" % title)

    out("      ▶ 正在播放：%s" % (title or ""))
    out("        【按 q 或 Esc 停止播放，空格 暂停/继续】")

    offset = 0.0            # 当前播放位置（秒）
    stopped = False
    first = True

    while True:
        proc = _spawn_ffplay(cfg, url, offset, title)
        if proc is None:
            return False
        if first:
            _drain_keys(debug)          # 丢掉播放前残留的按键
            first = False
        started = time.time()

        pause_requested = False
        while proc.poll() is None:
            try:
                ok, ch, special = _read_key()
            except KeyboardInterrupt:
                stopped = True
                break
            if ok:
                if debug:
                    _playback_debug("key %r special=%s" % (ch, special))
                if not special and ch in ("q", "Q", "\x1b"):
                    stopped = True
                    break
                if not special and ch == " ":
                    pause_requested = True
                    break
            time.sleep(0.03)

        played = time.time() - started
        offset += played
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

        if stopped:
            out("      ■ 已停止播放")
            if debug:
                _playback_debug("stopped at offset %.1fs" % offset)
            return True
        if pause_requested:
            out("      ⏸ 已暂停（按空格继续，按 q 停止）")
            if debug:
                _playback_debug("paused at offset %.1fs" % offset)
            # 停在暂停状态，等待继续/停止
            while True:
                try:
                    ok, ch, special = _read_key()
                except KeyboardInterrupt:
                    out("      ■ 已停止播放")
                    return True
                if ok:
                    if debug:
                        _playback_debug("paused-key %r special=%s" % (ch, special))
                    if not special and ch in ("q", "Q", "\x1b"):
                        out("      ■ 已停止播放")
                        return True
                    if not special and ch == " ":
                        out("      ▶ 继续播放")
                        break
                time.sleep(0.03)
            continue
        out("      ■ 播放结束")
        return True


def _ffplay_input_pipe(proc):
    """（已废弃，保留仅为兼容；新实现见 _make_ffplay_stdin）"""
    return _make_ffplay_stdin(proc)


# ----------------------------------------------------------------- 交互输入
HELP_TEXT = ("可用操作：直接输入序号选择歌曲 | :n 下一页 | :l 重看当前列表 | "
             ":s 重新搜索 | :id 歌曲ID 直接下载 | :h 帮助 | :q 退出")


def normalize_input(text):
    """把中文全角符号/字母转成半角，并去掉首尾空白。

    很多中文输入法会打出全角的「：」「ｓ」，直接比较 ':s' 会失配，
    这里统一成半角，命令写 :s / ：s / Ｓ 都能用。
    """
    if not text:
        return ""
    try:
        import unicodedata
        text = unicodedata.normalize("NFKC", text)
    except Exception:
        pass
    return text.strip()


def parse_command(raw):
    """解析用户输入，返回 (类型, 参数)。类型：quit/help/next/search/id/num/unknown/empty"""
    text = normalize_input(raw)
    if not text:
        return "empty", None
    low = text.lower().lstrip(":：").strip()
    if low in ("q", "quit", "exit", "退出"):
        return "quit", None
    if low in ("h", "help", "?", "帮助"):
        return "help", None
    if low in ("n", "next", "下一页"):
        return "next", None
    if low in ("s", "search", "重新搜索", "新搜索"):
        return "search", None
    if low in ("l", "list", "列表", "重看"):
        return "list", None
    if low.startswith("id") or low.startswith("i "):
        parts = low.replace(":", " ").split()
        for part in parts[1:]:
            if part.isdigit():
                return "id", int(part)
        return "id", None
    if text.isdigit():
        return "num", int(text)
    return "unknown", text


def ask(prompt):
    """读取一行输入；提示语自动补一个空格，避免和用户输入粘在一起。"""
    text = prompt if (not prompt or prompt.endswith((" ", ">", "："))) else prompt + " "
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        out("")
        return ":q"


def validate_download_dir(cfg):
    d = Path(cfg.get("download_dir") or DEFAULT_CONFIG["download_dir"])
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".ncmd_write_test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return d, True
    except Exception as e:
        out("[!] 下载目录不可写：%s（%s）" % (d, e))
        return d, False


# ----------------------------------------------------------------- 主流程
def do_download(cfg, api, song, opener, download_dir):
    """解析 -> 下载音频 + 歌词。song 至少包含 id/name/artists。"""
    sid = int(song["id"])
    name = song.get("name") or "未知歌曲"
    artist = song.get("artists") or song.get("singer") or "未知歌手"
    album = song.get("album") or ""

    out("\n[3/4] 正在解析歌曲（id=%d，音质优先：%s）..."
        % (sid, "、".join(QUALITY_LABEL_BY_CODE.get(c, c) for c in resolve_quality(cfg))))

    # 若来自接口的元数据可用，用它补全缺失信息
    try:
        info = api.song_info(sid)
        if info:
            name = song.get("name") or info.get("name") or name
            artist = song.get("artists") or info.get("singer") or artist
            album = song.get("album") or info.get("album") or album
    except Exception:
        pass

    url, url_info, errors = api.resolve_url(sid, resolve_quality(cfg))
    if not url:
        out("      [×] 解析失败，没有拿到音频直链：")
        for e in errors:
            out("          - %s" % e)
        out("      · 常见原因：该歌曲需要 VIP/数字专辑，解析站的公共账号无权获取；")
        out("        或该曲目已下架、版权受限。可换其他版本（如翻唱/现场版）再试。")
        return False

    level_label = QUALITY_LABEL_BY_CODE.get(url_info.get("level") or "", url_info.get("level") or "未知")
    ext = ".flac" if ".flac" in url.lower() else (".m4a" if ".m4a" in url.lower() else ".mp3")
    size = url_info.get("size") or 0
    out("      √ 解析成功：%s / %s" % (level_label, human_size(size)))
    out("      直链：%s" % (url[:110] + ("..." if len(url) > 110 else "")))

    stem = build_filename(cfg, name, artist)
    audio_dest = download_dir / stem
    # 已下载过就跳过（音质不同会导致扩展名不同，所以按「主名 + 音频扩展名」整体判断）
    existing = sorted(p for p in download_dir.glob(stem + ".*")
                      if p.suffix.lower() in (".mp3", ".flac", ".m4a", ".ogg"))
    audio_path, got, audio_ok = None, 0, None
    if existing and cfg.get("skip_existing", True):
        ex = max(existing, key=lambda p: p.stat().st_size)
        ex_size = ex.stat().st_size
        if ex_size > 1024 and (not size or abs(ex_size - size) < 1024 or ex_size >= size):
            out("      [=] 已存在同名音频，跳过下载：%s（%s）" % (ex.name, human_size(ex_size)))
            audio_path, got, audio_ok = ex, ex_size, True
        else:
            out("      [!] 已存在 %s，但大小与目标不符（%s / 目标 %s），将重新下载"
                % (ex.name, human_size(ex_size), human_size(size) if size else "未知"))
    if existing and not cfg.get("skip_existing", True):
        out("      [i] skip_existing = false，将覆盖已有文件")

    out("\n[4/4] 开始下载到 %s" % download_dir)
    if audio_ok is None:
        try:
            audio_path, got = download_file(opener, url, audio_dest, expected_size=size)
            audio_ok = True
            out("      √ 音乐已保存：%s（%s）" % (audio_path.name, human_size(got)))
        except Exception as e:
            out("      [×] 音频下载失败：%s" % e)
            return False

    # 歌词
    lrc_path = None
    try:
        ldata = api.lyric(sid)
        lrc_text, srt_text, n_lines = lrcutil.build(
            ldata.get("lrc") or "", ldata.get("tlyric") or "", ldata.get("yrc") or "",
            include_translation=cfg.get("include_translation", True))
        if lrc_text.strip():
            lrc_path = download_dir / (stem + ".lrc")
            lrc_path.write_text(lrc_text, encoding="utf-8")
            out("      √ 歌词已保存：%s（%d 行）" % (lrc_path.name, n_lines))
            if cfg.get("write_srt"):
                srt_path = download_dir / (stem + ".srt")
                srt_path.write_text(srt_text, encoding="utf-8")
                out("      √ 字幕已保存：%s" % srt_path.name)
        else:
            out("      · 该歌曲没有可用歌词（纯音乐或无歌词数据）。")
    except Exception as e:
        out("      [!] 歌词获取失败：%s" % e)

    if audio_ok and cfg.get("tag_metadata", True):
        try:
            if tag_audio(cfg, audio_path, name, artist, album):
                out("      √ 已写入歌曲信息标签（标题/歌手/专辑）")
        except Exception:
            pass

    out("")
    rule("=")
    out(" 下载完成")
    rule("=")
    out("  歌曲：%s - %s" % (name, artist))
    out("  音质：%s" % level_label)
    out("  目录：%s" % download_dir)
    if audio_ok:
        out("  音乐：%s（%s）" % (audio_path.name, human_size(audio_path.stat().st_size)))
    if lrc_path:
        out("  歌词：%s" % lrc_path.name)
    rule("=")
    return True


def interactive(cfg, opener, jar, force_login=False):
    out("")
    rule("=")
    out(" 网易云音乐 命令行下载工具  ncmd")
    out(" 搜索 → 试听 → 确认 → 下载（音乐 + 歌词）")
    rule("=")

    download_dir, writable = validate_download_dir(cfg)
    if not writable:
        alt = APP_DIR / "downloads"
        out("[i] 将改用备用目录：%s" % alt)
        cfg["download_dir"] = str(alt)
        download_dir, writable = validate_download_dir(cfg)

    levels = resolve_quality(cfg)
    out("[i] 下载目录：%s" % download_dir)
    out("[i] 音质顺序：%s" % " > ".join(QUALITY_LABEL_BY_CODE.get(c, c) for c in levels))
    player = find_player(cfg)
    out("[i] 播放器：%s" % (player or "未找到 ffplay（试听功能不可用）"))

    if cfg.get("login_on_start", True) or force_login:
        ensure_login(opener, jar, COOKIE_PATH, force=force_login, cfg=cfg)
    else:
        nick = check_login(opener)
        out("[1/4] 已跳过登录（login_on_start = false）%s"
            % ("，当前登录态：%s" % nick if nick else "，当前为匿名模式"))

    api = WyyApi(cfg, opener)
    out("\n[2/4] 输入歌名开始搜索（输入 :q 或直接回车退出）：")

    last_songs, page, keyword = [], 1, ""
    while True:
        if not last_songs:
            raw = ask("\n搜索歌曲 > ")
            kind, value = parse_command(raw)
            if kind in ("empty", "quit"):
                break
            if kind == "help":
                out("[i] " + HELP_TEXT)
                continue
            keyword = raw.strip()
            try:
                last_songs, total = search_songs(opener, keyword, cfg.get("search_limit", 20), 0)
            except Exception as e:
                out("[!] 搜索失败：%s" % e)
                continue
            if not last_songs:
                out("[!] 没有搜索到「%s」，换个关键词试试。" % keyword)
                continue
            page = 1
            out("\n找到约 %d 首，显示第 1 页（共 %d 首）：" % (total, len(last_songs)))
            print_songs(last_songs, page, cfg.get("search_limit", 20))
            continue

        raw = ask("\n选择序号 > ")
        kind, value = parse_command(raw)

        if kind in ("empty", "quit"):
            break
        if kind == "help":
            out("[i] " + HELP_TEXT)
            continue
        if kind == "next":
            try:
                off = page * cfg.get("search_limit", 20)
                more, total = search_songs(opener, keyword, cfg.get("search_limit", 20), off)
                if not more:
                    out("[i] 没有下一页了。")
                    out("[i] " + HELP_TEXT)
                    continue
                page += 1
                last_songs = more
                out("\n第 %d 页：" % page)
                print_songs(last_songs, page, cfg.get("search_limit", 20))
            except Exception as e:
                out("[!] 翻页失败：%s" % e)
            continue
        if kind == "search":
            last_songs = []
            out("[i] 好的，请输入新的歌名。")
            continue
        if kind == "list":
            print_songs(last_songs, page, cfg.get("search_limit", 20))
            continue
        if kind == "id":
            if value:
                song = {"id": value, "name": "", "artists": "", "album": ""}
                try:
                    info = api.song_info(value)
                    song["name"] = info.get("name") or ("歌曲 %d" % value)
                    song["artists"] = info.get("singer") or ""
                    song["album"] = info.get("album") or ""
                except Exception:
                    song["name"] = "歌曲 %d" % value
                _handle_selection(cfg, api, opener, song, download_dir, levels)
                out("[i] " + HELP_TEXT)
            else:
                out("[i] 用法：:id 421486605")
            continue
        if kind == "num":
            idx = value
            if not (1 <= idx <= len(last_songs)):
                out("[i] 序号超出范围（1 - %d）" % len(last_songs))
                out("[i] " + HELP_TEXT)
                continue
            _handle_selection(cfg, api, opener, last_songs[idx - 1], download_dir, levels)
            out("[i] 可以继续选择，或使用下面的操作：")
            out("[i] " + HELP_TEXT)
            continue

        # 既不是序号也不是命令：可能是用户直接输入了新的歌名
        out("[i] 「%s」不是序号也不是命令。" % raw)
        if len(raw) >= 2:
            out("[i] 想搜索它的话输入 :s 再输入歌名；想重看列表输入 :l；退出输入 :q")
            ans = ask("      现在就用「%s」搜索吗？(y/n，默认 y) > " % raw).lower()
            if ans in ("", "y", "yes", "是"):
                last_songs = []
                keyword_pending = raw
                try:
                    last_songs, total = search_songs(opener, keyword_pending,
                                                     cfg.get("search_limit", 20), 0)
                except Exception as e:
                    out("[!] 搜索失败：%s" % e)
                    continue
                if last_songs:
                    keyword, page = keyword_pending, 1
                    out("\n找到约 %d 首：" % total)
                    print_songs(last_songs, page, cfg.get("search_limit", 20))
                else:
                    out("[!] 没有搜索到「%s」。" % keyword_pending)
        else:
            out("[i] " + HELP_TEXT)

    out("\n已退出。音乐与歌词保存在：%s" % download_dir)


def _handle_selection(cfg, api, opener, song, download_dir, levels):
    """选中歌曲 -> 试听 -> 确认 -> 下载。"""
    sid = int(song["id"])
    name = song.get("name") or ("歌曲 %d" % sid)
    artist = song.get("artists") or ""
    out("")
    rule("-")
    out(" 已选择：[%d] %s%s" % (sid, name, (" - " + artist) if artist else ""))
    if song.get("album"):
        out(" 专辑：%s" % song["album"])
    rule("-")

    url, url_info, errors = (None, {}, [])
    if cfg.get("open_player_on_select", True):
        out(" 正在获取试听直链 ...")
        url, url_info, errors = api.resolve_url(sid, levels)
        if url:
            label = QUALITY_LABEL_BY_CODE.get(url_info.get("level") or "", "未知音质")
            out(" 试听音质：%s（%s）" % (label, human_size(url_info.get("size") or 0)))
            play_stream(cfg, url, "%s - %s" % (name, artist))
        else:
            out(" [!] 无法播放：没取到直链")
            for e in errors:
                out("     - %s" % e)
    else:
        out(" 已关闭自动试听（config.json 里 open_player_on_select = false）")

    ans = ask("\n是否确认下载这首歌曲？（y = 下载 / n = 取消）> ").lower()
    if ans not in ("y", "yes", "是", "1"):
        out(" 已取消。")
        return False
    return do_download(cfg, api, song, opener, download_dir)


# ----------------------------------------------------------------- CLI
def cmd_search(args, cfg, opener):
    keyword = args.search
    songs, total = search_songs(opener, keyword, args.limit, 0)
    out("关键词「%s」共找到约 %d 首，显示 %d 首：" % (keyword, total, len(songs)))
    print_songs(songs, 1, args.limit)
    return 0


def cmd_download(args, cfg, opener):
    api = WyyApi(cfg, opener)
    download_dir, _ = validate_download_dir(cfg)
    if args.id:
        sid = int(args.id)
        song = {"id": sid, "name": "", "artists": "", "album": ""}
        try:
            info = api.song_info(sid)
            song["name"] = info.get("name") or ("歌曲 %d" % sid)
            song["artists"] = info.get("singer") or ""
            song["album"] = info.get("album") or ""
        except Exception:
            song["name"] = "歌曲 %d" % sid
        return 0 if do_download(cfg, api, song, opener, download_dir) else 1
    if args.name:
        songs, _ = search_songs(opener, args.name, args.limit, 0)
        if not songs:
            out("[!] 没有搜索到「%s」" % args.name)
            return 1
        print_songs(songs, 1, args.limit)
        pick = args.pick
        if pick is None:
            pick = 1
        if not (1 <= pick <= len(songs)):
            out("[!] --pick 超出范围")
            return 1
        return 0 if do_download(cfg, api, songs[pick - 1], opener, download_dir) else 1
    out("[!] 需要 --id 或 --name")
    return 2


def cmd_selftest(cfg, opener):
    out("")
    rule("=")
    out(" 自检：依赖 / 目录 / 接口 / 二维码")
    rule("=")
    ok = True

    out("[1] Python 版本：%s" % sys.version.split()[0])
    out("[2] 下载目录：%s" % cfg.get("download_dir"))
    d, writable = validate_download_dir(cfg)
    out("    可写：%s" % ("是" if writable else "否"))
    ok = ok and writable
    player = find_player(cfg)
    out("[3] 播放器 ffplay：%s" % (player or "未找到（试听不可用，下载不受影响）"))
    out("[4] ffmpeg（写标签用）：%s" % (shutil.which("ffmpeg") or "未找到（跳过标签写入）"))

    out("[5] 二维码自检：")
    try:
        mat = pyqr.make_matrix("https://music.163.com/login?codekey=selftest", ecc="M")
        lines = pyqr.render_lines(mat, quiet=2, half_block=True)
        out("    版本 %d（%dx%d 模块），渲染 %d 行 x %d 列"
            % ((len(mat) - 17) // 4, len(mat), len(mat), len(lines), len(lines[0])))
    except Exception as e:
        ok = False
        out("    [×] 二维码生成失败：%s" % e)

    out("[6] 网易云接口：")
    try:
        songs, total = search_songs(opener, "晴天", 3, 0)
        out("    搜索成功：%d 条结果，示例：%s" % (total, "；".join(
            "%s - %s" % (s["name"], s["artists"]) for s in songs[:3])))
    except Exception as e:
        ok = False
        out("    [×] 搜索失败：%s" % e)

    out("[7] 解析站接口：")
    api = WyyApi(cfg, opener)
    ip = api.client_ip()
    out("    /api/ip -> %s" % (ip or "失败"))
    try:
        done = 0
        for s in songs[:3]:
            d = api.song_url(s["id"], "standard")
            data = d.get("data") or {}
            out("    /api/getSongUrl id=%s code=%s url=%s"
                % (s["id"], d.get("code"), "有" if data.get("url") else "无"))
            done += 1
            if done >= 2:
                break
    except Exception as e:
        out("    [!] %s" % e)

    out("")
    out("自检结果：%s" % ("通过" if ok else "存在问题，见上方 [×]"))
    return 0 if ok else 1


def main(argv=None):
    enable_utf8()
    parser = argparse.ArgumentParser(
        description="网易云音乐命令行搜索/试听/下载工具（扫码登录 -> 搜索 -> 试听 -> 下载到 D:\\music）")
    parser.add_argument("--search", metavar="关键词", help="只搜索并列出结果")
    parser.add_argument("--download", metavar="歌名", help="按歌名搜索并下载第一首（配合 --pick）")
    parser.add_argument("--id", metavar="歌曲ID", help="直接按歌曲 ID 下载")
    parser.add_argument("--pick", type=int, metavar="N", help="配合 --download，选择第 N 个搜索结果")
    parser.add_argument("--quality", metavar="音质", help="本次使用的音质，如 无损音质 / 标准音质")
    parser.add_argument("--dir", metavar="目录", help="本次下载目录（默认取 config.json）")
    parser.add_argument("--limit", type=int, default=20, help="搜索结果条数（默认 20）")
    parser.add_argument("--no-play", action="store_true", help="选中后不试听，直接询问是否下载")
    parser.add_argument("--relogin", action="store_true", help="忽略本地 Cookie，重新登录（会提示选择登录方式）")
    parser.add_argument("--login", choices=["phone", "qr", "ask"], metavar="方式",
                        help="登录方式：phone=手机号，qr=二维码，ask=每次询问（默认）")
    parser.add_argument("--selftest", action="store_true", help="运行环境与接口自检，不进入交互")
    parser.add_argument("--interactive", action="store_true", help="强制进入交互模式")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.quality:
        cfg["quality"] = args.quality
    if args.dir:
        cfg["download_dir"] = args.dir
    if args.limit:
        cfg["search_limit"] = args.limit
    if args.no_play:
        cfg["open_player_on_select"] = False
    if args.login:
        cfg["login_method"] = {"phone": "1", "qr": "2", "ask": "ask"}[args.login]

    opener, jar = build_opener(COOKIE_PATH)

    if args.selftest:
        return cmd_selftest(cfg, opener)

    if args.search:
        # 搜索接口匿名也能用；只有 --relogin 时才需要登录
        if args.relogin:
            ensure_login(opener, jar, COOKIE_PATH, force=True, required=True, cfg=cfg)
        elif not check_login(opener):
            out("[i] 未登录（匿名模式继续）。需要登录态可加 --relogin。")
        return cmd_search(args, cfg, opener)

    if args.download or args.id:
        # 按 ID/歌名直接下载时，登录不是必需的：解析接口用的是它自己的账号池。
        if args.relogin:
            ensure_login(opener, jar, COOKIE_PATH, force=True, cfg=cfg)
        elif not check_login(opener):
            out("[i] 未登录（匿名模式继续）；下载不依赖登录态。")
        return cmd_download(args, cfg, opener)

    try:
        interactive(cfg, opener, jar, force_login=args.relogin)
    except KeyboardInterrupt:
        out("\n已中断。")
    except Exception as e:
        out("\n[×] 出错了：%s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
