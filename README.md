# B站视频下载器

Windows 桌面端的 B 站视频下载工具，PySide6 图形界面，一次填地址、选清晰度就能拿到音画完整的 MP4。

> 起因是想跑通 [Henryhaohao/Bilibili_video_download](https://github.com/Henryhaohao/Bilibili_video_download)（Apache-2.0），
> 但那是 2019 年的项目：`interface.bilibili.com` 加密接口已返回 403，`imageio.plugins.ffmpeg.download()`
> 和 `moviepy.editor` 也被上游移除，且只认 av 号不支持 BV 号。
> 本项目是**按同样的目标重新实现**的现行版本，未复制其代码。感谢原作者提供的思路。

## 特性

- 支持 **BV 号 / av 号 / 完整链接 / b23.tv 短链**
- 支持 **分P**：链接带 `?p=2` 只下该集，不带则下全集
- 清晰度可选：360P / 480P / 720P / 1080P（另有 1080P+ / 1080P60 / 4K，需登录或大会员）
- 走 **DASH** 取流，视频流与音频流分开下载后用 **ffmpeg `-c copy` 直接封装**，不重编码 —— 耗时就等于下载时间
- 实时速度与进度显示，支持中途停止，完成后自动打开文件夹
- 自动探测 ffmpeg（PATH / 程序同目录 / 常见安装位置）

## 环境要求

- Windows
- Python 3.9+
- ffmpeg（**必需**，用于音视频合流）

```bash
pip install -r requirements.txt
```

ffmpeg 三种装法任选：

1. 加入系统 PATH
2. 把 `ffmpeg.exe` 放到本程序同目录
3. `winget install Gyan.FFmpeg`

## 使用

```bash
python bilibili_video_downloader.py
```

在界面里填入视频地址 → 选清晰度 → 选保存目录 → 开始下载。

默认保存到 `C:\Users\<你>\Videos\bilibili\<视频标题>\`。

## 工作原理

1. `api.bilibili.com/x/web-interface/view` 取视频信息与 `cid`
2. `api.bilibili.com/x/player/playurl` 加 `fnval=4048` 取 **DASH** 流，拿到多档视频流和独立音频流
3. 分别下载后交给 ffmpeg `-c copy` 封装成 mp4

## 已知限制

- **1080P 及以上需要登录**。不带 Cookie 时服务端最高只给 480P，这是 B 站的服务端限制，不是代码问题。想上 1080P 需要携带登录后的 `SESSDATA`。
- **付费内容、会员专享、番剧** 无法下载，会明确报错。
- 请仅用于下载你有权保存的内容，遵守 B 站用户协议与著作权法。

## 免责声明

本项目仅供学习与技术交流使用。使用者应自行承担因使用本工具产生的一切后果，
包括但不限于版权纠纷与账号风险。请勿用于任何商业用途或侵权行为。

## License

MIT
