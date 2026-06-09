# paqu 视频爬取工具

一个小型命令行工具，用来发现网页 HTML 中公开暴露的视频链接，并可选下载直链视频文件。

它不会绕过登录、付费墙、DRM、CAPTCHA 或访问控制。请只在你有权限抓取和下载的网站上使用，并遵守目标网站的服务条款、`robots.txt` 和速率限制。

## 依赖

项目优先使用 [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) 识别和下载页面中的视频格式，并使用 [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) 作为普通页面直链和 HLS 的备用抓取引擎。首次使用前安装依赖：

```powershell
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install -r requirements.txt
```

## 使用

双击项目里的 `start_server.bat`，或在 PowerShell 里运行：

```powershell
.\start_server.bat
```

然后打开：

```text
http://127.0.0.1:8080
```

启动窗口需要保持打开，关闭后网页服务会停止。

启动本地网页工具：

```powershell
python web_app.py
```

如果你的 `python` 命令不可用，可以用 Codex 桌面自带的 Python：

```powershell
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe web_app.py
```

然后打开：

```text
http://127.0.0.1:8080
```

网页支持输入页面链接、扫描可下载格式、选择清晰度，并下载到项目内 `downloads/` 或你填写的保存目录。
下载时优先由 `yt-dlp` 合并为 `.mp4`；备用直链/HLS 下载完成后会用 `ffmpeg` 尽量转为 `.mp4`。项目依赖里的 `imageio-ffmpeg` 会提供可用的 ffmpeg。
扫描顺序是先让 `yt-dlp` 识别页面；如果 `yt-dlp` 没有识别出可下载格式，才会进入受 `robots.txt` 约束的 Scrapling 备用页面抓取。

命令行模式仍然可用：

```powershell
python video_crawler.py "https://example.com/page"
```

输出 JSON：

```powershell
python video_crawler.py "https://example.com/page" --json
```

下载发现到的直链视频：

```powershell
python video_crawler.py "https://example.com/page" --download --output-dir downloads
```

如果你的 `python` 命令不可用，可以用 Codex 桌面自带的 Python：

```powershell
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe video_crawler.py "https://example.com/page"
```

## 支持范围

- `<video src="...">`
- `<source src="..." type="video/...">`
- 常见属性：`src`、`href`、`data-src`、`data-url`、`data-video`、`content`
- HTML 文本中的 `.mp4`、`.webm`、`.mov`、`.m4v`、`.avi`、`.mkv`、`.m3u8`、`.mpd`
- HLS `.m3u8` master playlist 中的 `RESOLUTION`、`BANDWIDTH`、`CODECS` 清晰度信息

## 下载限制

- 普通直链视频按 `original` 下载。
- HLS 仅支持未加密、普通 `.ts` 分片 playlist，下载后会转为 `.mp4`。
- 加密 HLS、DRM、复杂 fMP4 HLS 暂不支持，也不会尝试绕过访问控制。
- 不使用 cookies、登录态、付费墙、DRM 绕过或其他访问控制绕过方式。请只下载你有权限保存的内容。

## 下一步可扩展

- 针对具体网站补选择器或 JSON-LD 解析
- 加入 JavaScript 渲染页面支持
- 对 `.m3u8` 做播放列表解析
- 增加去重、限速、失败重试和批量队列
