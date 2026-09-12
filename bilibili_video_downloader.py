# -*- coding: utf-8 -*-
"""
B站视频下载器 —— PySide6 桌面版

基于 Henryhaohao/Bilibili_video_download 的思路重写：
  - 原仓库（2019）使用的 interface.bilibili.com 加密接口已 403 失效，
    imageio.plugins.ffmpeg.download() / moviepy.editor 也已被上游移除，故不再沿用。
  - 改用现行的 x/player/playurl + fnval=4048（DASH 协议）拿流，
    再用本机 ffmpeg 合流成 mp4，音画完整且有多个清晰度可选。
  - 同时支持 AV 号 / BV 号 / 完整链接 / 分P 参数 ?p=N。

依赖：PySide6、requests
"""

import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import urllib.parse

import requests

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit, QProgressBar,
    QFileDialog, QMessageBox, QGroupBox, QCheckBox, QFrame, QDialog
)

APP_TITLE = "B站视频下载器"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/playurl"
API_QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
API_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# qn -> 名称
QUALITY_NAMES = {
    127: "8K 超高清", 126: "杜比视界", 125: "HDR 真彩", 120: "4K 超清",
    116: "1080P60 高帧率", 112: "1080P+ 高码率", 100: "智能修复",
    80: "1080P 高清", 74: "720P60 高帧率", 64: "720P 高清",
    32: "480P 清晰", 16: "360P 流畅", 6: "240P 极速",
}


# 配置存放位置（用户主目录，不会进仓库）
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".bili_downloader.json")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def find_ffmpeg():
    """按优先级找 ffmpeg；找不到返回 None。"""
    hits = []
    # 1) PATH
    p = shutil.which("ffmpeg")
    if p:
        hits.append(p)
    # 2) WorkBuddy 自带
    cand = r"C:\Users\Gu\.workbuddy\binaries\ffmpeg\bin\ffmpeg.exe"
    if os.path.isfile(cand):
        hits.append(cand)
    # 3) ChatCut 自带
    cand = (r"C:\Users\Gu\AppData\Local\Programs\ChatCut\resources"
            r"\app.asar.unpacked\node_modules\ffmpeg-ffprobe-static\ffmpeg.exe")
    if os.path.isfile(cand):
        hits.append(cand)
    # 4) 程序同目录
    cand = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "ffmpeg.exe")
    if os.path.isfile(cand):
        hits.append(cand)
    return hits[0] if hits else None


def sanitize(name):
    """去掉 Windows 文件名非法字符。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = name.strip(" .")
    return name[:120] or "untitled"


def normalize_cookie(text):
    """把用户粘贴的内容规整成可用的 Cookie 串。

    接受的输入：
      1. 完整 Cookie 字符串（含 SESSDATA=xxx；...）
      2. 只粘贴了 SESSDATA 的值
      3. 复制时带上引号 / 换行 / 多余空格的脏数据
    注意：SESSDATA 的值本身是 URL 编码的（含 %2C），不能 unquote。
    """
    t = (text or "").strip()
    if not t:
        return ""
    t = t.strip('"').strip("'").strip()
    # 整串被 URL 编码过的情况
    if t[:10].upper().startswith("SESSDATA%3D"):
        try:
            t = urllib.parse.unquote(t)
        except Exception:
            pass
    # 完整 cookie 串
    if re.search(r"(^|;\s*)SESSDATA\s*=", t, re.I):
        t = re.sub(r"[\r\n]+", " ", t)
        parts = [p.strip() for p in t.split(";") if p.strip()]
        return "; ".join(parts)
    # 只有 SESSDATA 值
    t = re.sub(r"^\s*SESSDATA\s*=\s*", "", t, flags=re.I)
    return "SESSDATA=" + t.strip()


def pick_video_stream(vs, qn, prefer=None):
    """从 DASH 视频流列表里挑一条，返回 (选中流, 同清晰度的候选池)。

    规则：
      1. 先取「不超过 qn 的最高清晰度」那一档；
      2. 同一档内 B站可能同时给 avc1 / hev1 / av01 三份独立转码，码率差很多
         （实测同档 avc1 1788 kbps，hev1 仅 589 kbps）。
         默认按**码率最高**取 —— 这才是最接近网页端播放的画质；
         传了 prefer（如 ["avc1","av01"]）才按编码优先级挑。
    """
    cand = [v for v in vs if v["id"] <= qn] or vs
    best_id = max(v["id"] for v in cand)
    pool = [v for v in cand if v["id"] == best_id]
    if prefer:
        for codec in prefer:
            for v in pool:
                if v["codecs"].startswith(codec):
                    return v, pool
    return max(pool, key=lambda v: v.get("bandwidth") or 0), pool


class QrLogin(QThread):
    """扫码登录：取二维码 -> 等用户扫 -> 拿到 SESSDATA。

    走的是 B站官方 passport 接口，全程不碰浏览器 Cookie。
    """
    qr_ready = Signal(bytes)     # 二维码 PNG 字节
    status = Signal(str)         # 状态文案
    ok = Signal(str, str)        # 成功：(cookie 串, 用户名)
    fail = Signal(str)           # 失败原因

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        import io
        try:
            import qrcode
        except ImportError:
            self.fail.emit("缺少 qrcode 库，请先运行：pip install qrcode pillow")
            return

        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})

        self.status.emit("正在获取二维码…")
        try:
            j = s.get(API_QR_GEN, timeout=20).json()
        except Exception as e:
            self.fail.emit("网络错误：%s" % e)
            return
        if j.get("code") != 0:
            self.fail.emit("获取二维码失败：%s" % (j.get("message") or j.get("code")))
            return

        d = j["data"]
        key = d["qrcode_key"]
        try:
            img = qrcode.make(d["url"])
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self.qr_ready.emit(buf.getvalue())
        except Exception as e:
            self.fail.emit("生成二维码失败：%s" % e)
            return

        self.status.emit("请用手机 B站 App 扫码 → 再点「确认登录」")

        last = None
        deadline = time.time() + 180
        while time.time() < deadline and not self._stop:
            try:
                rr = s.get(API_QR_POLL, params={"qrcode_key": key},
                           timeout=20, allow_redirects=False)
                jj = rr.json()
                dd = jj.get("data") or {}
                code = dd.get("code")
                if code != last:
                    last = code
                    if code == 86101:
                        self.status.emit("等待扫码…")
                    elif code == 86090:
                        self.status.emit("已扫码，请在手机上点「确认登录」")
                    elif code == 86038:
                        self.fail.emit("二维码已过期，请重新点「扫码登录」")
                        return
                if code == 0:
                    sess = None
                    for ck in rr.cookies:
                        if ck.name == "SESSDATA":
                            sess = ck.value
                    if not sess:
                        m = re.search(r"SESSDATA=([^;]+)",
                                      rr.headers.get("Set-Cookie", ""))
                        if m:
                            sess = m.group(1)
                    if sess:
                        cookie = "SESSDATA=" + sess
                        uname = ""
                        try:
                            nv = requests.Session()
                            nv.headers.update({"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com/",
                                               "Cookie": cookie})
                            nd = nv.get("https://api.bilibili.com/x/web-interface/nav",
                                        timeout=15).json().get("data") or {}
                            if nd.get("isLogin"):
                                uname = nd.get("uname") or ""
                                if nd.get("vipStatus"):
                                    uname += "（大会员）"
                        except Exception:
                            pass
                        self.ok.emit(cookie, uname)
                    else:
                        self.fail.emit("登录成功，但没在响应里拿到 SESSDATA")
                    return
            except Exception:
                pass
            time.sleep(2)

        if not self._stop:
            self.fail.emit("超时，请重新点「扫码登录」")


class BiliApi:
    def __init__(self, cookie=""):
        self.cookie = normalize_cookie(cookie)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        if self.cookie:
            self.s.headers.update({"Cookie": self.cookie})

    # ---------- 解析用户输入 ----------
    @staticmethod
    def parse_input(text):
        """返回 (kind, value, page)。kind ∈ {'aid','bvid'}；page 为 1 起的分P号或 None。"""
        text = (text or "").strip()
        if not text:
            return None, None, None

        page = None
        m = re.search(r"[?&]p=(\d+)", text)
        if m:
            page = int(m.group(1))

        # 纯数字 -> av 号
        if re.fullmatch(r"\d+", text):
            return "aid", text, page

        # BV 号
        m = re.search(r"(BV[0-9A-Za-z]{10})", text)
        if m:
            return "bvid", m.group(1), page

        # av 号（链接里）
        m = re.search(r"/av(\d+)", text, re.I)
        if m:
            return "aid", m.group(1), page

        # 短链
        if "b23.tv" in text:
            return "short", text, page

        return None, None, page

    def resolve_short(self, url):
        r = self.s.get(url, allow_redirects=True, timeout=15)
        return self.parse_input(r.url)

    # ---------- 视频信息 ----------
    def view(self, kind, value):
        params = {"aid": value} if kind == "aid" else {"bvid": value}
        j = self.s.get(API_VIEW, params=params, timeout=20).json()
        if j.get("code") != 0:
            raise RuntimeError("获取视频信息失败：%s（code=%s）" % (j.get("message"), j.get("code")))
        return j["data"]

    def playurl(self, bvid, cid, qn=80):
        """fnval=4048 = DASH + 全部特性。返回 data 字典。"""
        params = {"bvid": bvid, "cid": cid, "qn": qn,
                  "fnval": 4048, "fourk": 1, "platform": "pc", "high_quality": 1}
        j = self.s.get(API_PLAYURL, params=params, timeout=20).json()
        if j.get("code") != 0:
            raise RuntimeError("获取播放地址失败：%s（code=%s）" % (j.get("message"), j.get("code")))
        return j["data"]


class Downloader(QThread):
    log = Signal(str)
    progress = Signal(int)          # 0-100，整体
    stage = Signal(str)
    done = Signal(bool, str)

    def __init__(self, link, qn, outdir, ffmpeg, prefer=None, only_audio=False,
                 cookie="", parent=None):
        super().__init__(parent)
        self.link = link
        self.qn = qn
        self.outdir = outdir
        self.ffmpeg = ffmpeg
        # 编码偏好；None / 空列表 = 同一清晰度里按**码率最高**取（推荐）
        self.prefer = prefer
        self.only_audio = only_audio
        self.cookie = normalize_cookie(cookie)     # 登录态，空串=未登录
        self._stop = False

    def stop(self):
        self._stop = True

    # ---------- 内部工具 ----------
    def _download(self, url, path, start_pct, end_pct, label):
        headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        with self.s_get(url, headers, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            t0 = time.time()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if self._stop:
                        raise RuntimeError("已取消")
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        frac = got / total
                        pct = int(start_pct + (end_pct - start_pct) * frac)
                        self.progress.emit(pct)
                        spd = got / max(time.time() - t0, 0.01) / 1024 / 1024
                        self.stage.emit("%s  %.1f%%  %.1f MB/s" % (
                            label, frac * 100, spd))
        return path

    @staticmethod
    def s_get(url, headers, stream=False):
        return requests.get(url, headers=headers, stream=stream, timeout=60)

    def _run_ffmpeg(self, args, stage_label):
        self.log.emit("$ ffmpeg " + " ".join(
            ('"%s"' % a if " " in a else a) for a in args))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=flags)
        tail = []
        for raw in p.stdout:
            if self._stop:
                p.kill()
                raise RuntimeError("已取消")
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                tail.append(line)
                tail = tail[-30:]
        p.wait()
        if p.returncode != 0:
            raise RuntimeError("%s 失败：\n%s" % (stage_label, "\n".join(tail[-10:])))
        return tail

    # ---------- 主流程 ----------
    def run(self):
        tmpfiles = []
        try:
            api = BiliApi(self.cookie)
            kind, value, page = api.parse_input(self.link)
            if kind == "short":
                self.stage.emit("解析短链…")
                kind, value, page = api.resolve_short(value)
            if not kind:
                raise RuntimeError("无法识别输入，请填 av号 / BV号 / 完整链接")

            self.stage.emit("获取视频信息…")
            info = api.view(kind, value)
            bvid = info["bvid"]
            title = sanitize(info["title"])
            pages = info.get("pages") or []
            if page is None:
                targets = pages
            else:
                if page < 1 or page > len(pages):
                    raise RuntimeError("分P 超出范围（共 %d 集）" % len(pages))
                targets = [pages[page - 1]]

            multi = len(targets) > 1
            self.log.emit("视频：%s" % info["title"])
            self.log.emit("BV号：%s   共 %d 集" % (bvid, len(pages)))
            self.log.emit("登录态：%s" % ("✔ 已填 Cookie，可获取高清晰度"
                                          if self.cookie else
                                          "✘ 未填 Cookie —— 最高只能拿 480P"))

            outroot = os.path.join(self.outdir, title)
            os.makedirs(outroot, exist_ok=True)

            ok_all = True
            for idx, item in enumerate(targets):
                if self._stop:
                    raise RuntimeError("已取消")
                cid = str(item["cid"])
                num = item.get("page", idx + 1)
                part = sanitize(item.get("part") or title)
                base_pct = int(idx / len(targets) * 100)
                span = int(100 / len(targets))

                self.log.emit("\n—— 第 %d 集：%s ——" % (num, part))
                self.stage.emit("获取流地址…")
                data = api.playurl(bvid, cid, self.qn)

                # 服务端实际提供哪些档位由片源决定，直接列出来，省得用户以为还能更高
                aq = data.get("accept_quality") or []
                if aq:
                    best = max(aq)
                    self.log.emit("  该视频可用清晰度：%s（最高 %s）" % (
                        " / ".join(QUALITY_NAMES.get(q, str(q)) for q in aq),
                        QUALITY_NAMES.get(best, str(best))))

                dash = data.get("dash")
                if dash:
                    vs = dash.get("video", [])
                    aus = dash.get("audio") or []
                    if not vs:
                        raise RuntimeError("没有可用的视频流")
                    # 见模块上方 pick_video_stream()：默认取同清晰度里码率最高的那份转码
                    chosen, pool = pick_video_stream(vs, self.qn, self.prefer)

                    kb = lambda v: int((v.get("bandwidth") or 0) / 1000)
                    self.log.emit("画质：%s  %sx%s  %s  码率 %s kbps" % (
                        QUALITY_NAMES.get(chosen["id"], str(chosen["id"])),
                        chosen["width"], chosen["height"], chosen["codecs"], kb(chosen)))
                    if len(pool) > 1:
                        alt = "  ".join("%s %dkbps" % (v["codecs"].split(".")[0], kb(v))
                                        for v in sorted(pool, key=lambda x: -(x.get("bandwidth") or 0)))
                        self.log.emit("  同档可选编码：%s（已选最高）" % alt)
                    if chosen["id"] < self.qn:
                        name_low = QUALITY_NAMES.get(chosen["id"], str(chosen["id"]))
                        if not self.cookie:
                            self.log.emit("⚠ 只给到 %s：未登录时 B站服务端最高只发 480P，"
                                          "不是程序的问题。" % name_low)
                            self.log.emit("  → 要 1080P：在「登录 Cookie」填 SESSDATA 后重下。")
                        else:
                            self.log.emit("⚠ 只给到 %s —— 多数情况是**该片源本身没有更高档**"
                                          "（UP 主没传），少数是需大会员。" % name_low)
                            self.log.emit("  → 上限就是上面那句「最高 ○○」，下不到更高的了。")

                    vpath = os.path.join(outroot, ".%s.v.m4s" % num)
                    apath = os.path.join(outroot, ".%s.a.m4s" % num)
                    tmpfiles += [vpath, apath]

                    self._download(chosen["baseUrl"], vpath,
                                   base_pct, base_pct + int(span * 0.55),
                                   "视频流 %s" % ("%.0fP" % chosen["height"]))
                    if self.only_audio and aus:
                        pass
                    if aus:
                        best_a = max(aus, key=lambda a: a.get("bandwidth", 0))
                        self._download(best_a["baseUrl"], apath,
                                       base_pct + int(span * 0.55),
                                       base_pct + int(span * 0.85),
                                       "音频流")
                    else:
                        apath = None

                    outname = ("%02d %s.mp4" % (num, part)) if multi else (part + ".mp4")
                    outpath = os.path.join(outroot, outname)
                    self.stage.emit("ffmpeg 合流…")
                    if apath:
                        args = [self.ffmpeg, "-y", "-loglevel", "error",
                                "-i", vpath, "-i", apath,
                                "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
                                outpath]
                    else:
                        args = [self.ffmpeg, "-y", "-loglevel", "error",
                                "-i", vpath, "-c", "copy", outpath]
                    self._run_ffmpeg(args, "合流")
                    self.log.emit("完成：%s" % outpath)
                else:
                    # 回退：整段 durl（flv）
                    durl = data.get("durl") or []
                    if not durl:
                        raise RuntimeError("接口未返回可下载流（可能是会员专享或需登录）")
                    url = durl[0]["url"]
                    ext = ".flv"
                    tmp = os.path.join(outroot, ".%s.all%s" % (num, ext))
                    tmpfiles.append(tmp)
                    self._download(url, tmp, base_pct,
                                   base_pct + int(span * 0.85), "整段流")
                    outname = ("%02d %s.mp4" % (num, part)) if multi else (part + ".mp4")
                    outpath = os.path.join(outroot, outname)
                    self.stage.emit("转封装…")
                    self._run_ffmpeg([self.ffmpeg, "-y", "-loglevel", "error",
                                      "-i", tmp, "-c", "copy", outpath], "转封装")
                    self.log.emit("完成：%s" % outpath)

                self.progress.emit(min(100, base_pct + span))

            self.progress.emit(100)
            self.stage.emit("全部完成")
            self.done.emit(True, outroot)
        except Exception as e:
            self.stage.emit("失败")
            msg = str(e)
            self.log.emit("\n[错误] " + msg)
            self.done.emit(False, msg)
        finally:
            for f in tmpfiles:
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except OSError:
                    pass


class QrDialog(QDialog):
    """显示二维码、等待扫码完成的模态小窗。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("扫码登录 B站")
        self.setFixedSize(324, 420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        tip = QLabel("手机 B站 App →「扫一扫」")
        tip.setAlignment(Qt.AlignCenter)
        tip.setStyleSheet("font-weight:600;")
        lay.addWidget(tip)

        self.lbl_qr = QLabel("正在获取…")
        self.lbl_qr.setAlignment(Qt.AlignCenter)
        self.lbl_qr.setFixedSize(260, 260)
        self.lbl_qr.setStyleSheet(
            "border:1px solid #e0e0e0;border-radius:8px;color:#999;")
        lay.addWidget(self.lbl_qr, 0, Qt.AlignCenter)

        self.lbl_st = QLabel("")
        self.lbl_st.setAlignment(Qt.AlignCenter)
        self.lbl_st.setWordWrap(True)
        self.lbl_st.setStyleSheet("color:#666;")
        lay.addWidget(self.lbl_st)

        self.cookie = None
        self.uname = ""
        self.th = QrLogin(self)
        self.th.qr_ready.connect(self._on_qr)
        self.th.status.connect(self.lbl_st.setText)
        self.th.ok.connect(self._on_ok)
        self.th.fail.connect(self._on_fail)
        self.th.start()

    def _on_qr(self, png):
        pm = QPixmap()
        pm.loadFromData(png)
        if pm.isNull():
            self.lbl_st.setText("二维码渲染失败")
            return
        self.lbl_qr.setPixmap(pm.scaled(248, 248, Qt.KeepAspectRatio,
                                        Qt.FastTransformation))

    def _on_ok(self, cookie, uname):
        self.cookie = cookie
        self.uname = uname
        self.accept()

    def _on_fail(self, msg):
        self.lbl_st.setText("✗ " + msg)
        self.lbl_qr.setText("×")

    def closeEvent(self, e):
        self.th.stop()
        self.th.wait(3000)
        super().closeEvent(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(760, 620)
        self.ffmpeg = find_ffmpeg()
        self.worker = None
        self._build_ui()
        self._load_prefs()
        if not self.ffmpeg:
            self.log("⚠ 未找到 ffmpeg，无法合流。请把 ffmpeg.exe 放到程序同目录或加入 PATH。")
        else:
            self.log("✔ ffmpeg：%s" % self.ffmpeg)
        if normalize_cookie(self.inp_cookie.text()):
            self.log("✔ 已载入上次保存的 Cookie")

    # ---------- UI ----------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        title = QLabel(APP_TITLE)
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)

        sub = QLabel("支持 av号 / BV号 / 完整链接 / 分P（链接带 ?p=2）")
        sub.setStyleSheet("color:#888;")
        lay.addWidget(sub)

        # 输入区
        box = QGroupBox("下载设置")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("视频地址"), 0, 0)
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("BV1cb411V7Lm  或  https://www.bilibili.com/video/BV1cb411V7Lm?p=1")
        grid.addWidget(self.inp, 0, 1, 1, 3)

        grid.addWidget(QLabel("清晰度"), 1, 0)
        self.cmb = QComboBox()
        self.quality_items = [
            (80, "1080P 高清"), (64, "720P 高清"), (32, "480P 清晰"), (16, "360P 流畅"),
            (112, "1080P+ 高码率（需登录）"), (116, "1080P60（需大会员）"),
            (120, "4K 超清（需大会员）"),
        ]
        for qn, name in self.quality_items:
            self.cmb.addItem(name, qn)
        grid.addWidget(self.cmb, 1, 1)

        grid.addWidget(QLabel("登录 Cookie"), 2, 0)
        self.inp_cookie = QLineEdit()
        self.inp_cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_cookie.setPlaceholderText("填了才能下 1080P；点右边「扫码登录」最省事")
        grid.addWidget(self.inp_cookie, 2, 1)
        btn_qr = QPushButton("扫码登录")
        btn_qr.setFixedWidth(80)
        btn_qr.setToolTip("用手机 B站 App 扫码，自动获取登录凭据")
        btn_qr.clicked.connect(self.do_qr_login)
        grid.addWidget(btn_qr, 2, 2)
        btn_help = QPushButton("怎么填？")
        btn_help.setFixedWidth(80)
        btn_help.clicked.connect(self.show_cookie_help)
        grid.addWidget(btn_help, 2, 3)

        grid.addWidget(QLabel("保存到"), 3, 0)
        self.outdir = QLineEdit(os.path.join(os.path.expanduser("~"), "Videos", "bilibili"))
        grid.addWidget(self.outdir, 3, 1, 1, 2)
        btn_dir = QPushButton("选择…")
        btn_dir.setFixedWidth(80)
        btn_dir.clicked.connect(self.choose_dir)
        grid.addWidget(btn_dir, 3, 3)

        self.chk_open = QCheckBox("完成后打开文件夹")
        self.chk_open.setChecked(True)
        grid.addWidget(self.chk_open, 4, 1)

        self.chk_remember = QCheckBox("记住 Cookie")
        self.chk_remember.setChecked(True)
        self.chk_remember.setToolTip("存到 %s（仅本机，不会进仓库）" % CONFIG_PATH)
        grid.addWidget(self.chk_remember, 4, 2, 1, 2)

        lay.addWidget(box)

        # 按钮
        row = QHBoxLayout()
        self.btn_start = QPushButton("开始下载")
        self.btn_start.setFixedHeight(36)
        self.btn_start.setStyleSheet(
            "QPushButton{background:#00a1d6;color:white;border:none;border-radius:6px;"
            "font-size:14px;font-weight:600;}"
            "QPushButton:hover{background:#0b8fc4;}"
            "QPushButton:disabled{background:#cfcfcf;}")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setFixedHeight(36)
        self.btn_stop.setFixedWidth(90)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        lay.addLayout(row)

        # 进度
        self.bar = QProgressBar()
        self.bar.setValue(0)
        lay.addWidget(self.bar)

        self.stage_lbl = QLabel("就绪")
        self.stage_lbl.setStyleSheet("color:#555;")
        lay.addWidget(self.stage_lbl)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#ddd;")
        lay.addWidget(line)

        self.box = QTextEdit()
        self.box.setReadOnly(True)
        self.box.setStyleSheet(
            "QTextEdit{background:#fafafa;border:1px solid #e2e2e2;border-radius:6px;"
            "font-family:Consolas,'Microsoft YaHei',monospace;font-size:12px;}")
        lay.addWidget(self.box, 1)

    # ---------- 行为 ----------
    def _load_prefs(self):
        cfg = load_config()
        if cfg.get("cookie"):
            self.inp_cookie.setText(cfg["cookie"])
        if cfg.get("outdir"):
            self.outdir.setText(cfg["outdir"])
        if isinstance(cfg.get("qn"), int):
            i = self.cmb.findData(cfg["qn"])
            if i >= 0:
                self.cmb.setCurrentIndex(i)

    def _save_prefs(self, outdir, qn, cookie):
        """保存设置。

        注意：必须**先读回已有配置再改**，不能新建 dict 整体覆盖 ——
        否则任何一次「未填 Cookie 就点下载」都会把已保存的登录凭据抹掉。
        """
        prefs = load_config()
        prefs["outdir"] = outdir
        prefs["qn"] = qn
        if self.chk_remember.isChecked():
            if cookie:
                prefs["cookie"] = cookie
        else:
            prefs.pop("cookie", None)      # 取消勾选 = 明确不要保存
        save_config(prefs)

    def do_qr_login(self):
        dlg = QrDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.cookie:
            self.inp_cookie.setText(dlg.cookie)
            prefs = load_config()
            prefs["cookie"] = dlg.cookie
            save_config(prefs)
            self.chk_remember.setChecked(True)
            extra = ("　账号：%s" % dlg.uname) if dlg.uname else ""
            self.log("✔ 扫码登录成功，Cookie 已保存。%s" % extra)
        else:
            self.log("扫码登录未完成")

    def show_cookie_help(self):
        QMessageBox.information(self, "怎么获取登录凭据", (
            "【推荐】直接点「扫码登录」按钮，用手机 B站 App 扫一下就行，不用管 Cookie。\n"
            "——————————————————\n\n"
            "手动填写（备选）：\n\n"
            "1. 浏览器打开 bilibili.com，确认已登录\n\n"
            "2. 按 F12 打开开发者工具\n\n"
            "3. 顶部标签切到「存储 / Storage」（旧版叫「应用程序 / Application」）\n\n"
            "4. 左侧展开 Cookies → https://www.bilibili.com\n\n"
            "5. 找到名为 SESSDATA 的那一行，双击「值」列，全选复制\n\n"
            "6. 粘贴到「登录 Cookie」框（只贴值、或整条 Cookie 串都认）\n\n"
            "说明：SESSDATA 等同于你的登录凭证，只留在本机；"
            "勾了「记住 Cookie」会写入\n%s\n"
            "不要把这个值发给别人，也别截图公开。") % CONFIG_PATH)

    def log(self, s):
        self.box.append(s)
        self.box.verticalScrollBar().setValue(self.box.verticalScrollBar().maximum())

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.outdir.text())
        if d:
            self.outdir.setText(d)

    def start(self):
        link = self.inp.text().strip()
        if not link:
            QMessageBox.warning(self, "提示", "请先填写视频地址")
            return
        if not self.ffmpeg:
            QMessageBox.critical(self, "缺少 ffmpeg",
                                 "没找到 ffmpeg.exe，无法合流。\n"
                                 "把它放到程序同目录，或加入系统 PATH。")
            return
        outdir = self.outdir.text().strip()
        if not outdir:
            outdir = os.path.join(os.path.expanduser("~"), "Videos", "bilibili")
            self.outdir.setText(outdir)
        os.makedirs(outdir, exist_ok=True)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.bar.setValue(0)
        self.box.clear()

        qn = self.cmb.currentData()
        cookie = normalize_cookie(self.inp_cookie.text())
        if qn >= 80 and not cookie:
            self.log("⚠ 未填 Cookie：B站未登录最高只发 480P，选 1080P 也会被降级。")

        self._save_prefs(outdir, qn, cookie)

        self.worker = Downloader(link, qn, outdir, self.ffmpeg, cookie=cookie)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.bar.setValue)
        self.worker.stage.connect(self.stage_lbl.setText)
        self.worker.done.connect(self.on_done)
        self.worker.start()

    def stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.stage_lbl.setText("正在停止…")

    def on_done(self, ok, info):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if ok:
            self.stage_lbl.setText("已完成")
            if self.chk_open.isChecked() and os.path.isdir(info):
                try:
                    os.startfile(info)
                except OSError:
                    pass
        else:
            self.stage_lbl.setText("失败")
            if info and "已取消" not in info:
                QMessageBox.critical(self, "下载失败", info)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
