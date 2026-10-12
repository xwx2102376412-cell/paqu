# paqu 内容爬取工具

一个小型本地网页工具，用来发现网页中公开暴露的视频、图片和文字内容。视频可选择清晰度并下载，图片和文字会展示在扫描结果中方便复制。

它不会绕过登录、付费墙、DRM、CAPTCHA 或访问控制。请只在你有权限抓取和下载的网站上使用，并遵守目标网站的服务条款、`robots.txt` 和速率限制。

## 依赖

项目优先使用 [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) 识别和下载页面中的视频格式，并使用 [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) 抓取静态页面中的公开视频、图片和文字内容。`start_server.bat` 会自动寻找 Python 3.10+，并在首次启动时安装缺失的依赖。也可以手动安装：

```powershell
py -3 -m pip install -r requirements.txt
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

每日视频 Agent 审核台：

```text
http://127.0.0.1:8080/agent
```

双击 `start_server.bat` 后，服务监听成功会自动用默认浏览器打开 Agent 审核台。服务窗口需要保持开启。

## 每日视频发现 Agent

Agent 第一版只搜索 YouTube 的公开结果，默认检查近 30 天并在一次运行中最多保存 100 个唯一候选。内置主题为“船舶系泊绳断裂”“救援绳事故”“吊装带承重测试”，每个主题使用预设的中英文查询词。规则判定会把候选分成“高相关、待审核、不相关”，并显示命中词和中文理由。

### 可选：DeepSeek + Qwen 视觉判断

先在服务商控制台创建新 API Key。不要把 Key 发到聊天、截图或提交到 Git。复制配置模板：

```powershell
Copy-Item .env.example .env
notepad .env
```

在 `.env` 中填写 `DEEPSEEK_API_KEY` 和 `QWEN_API_KEY`。默认模型分别为 `deepseek-v4-flash` 与 `qwen3-vl-flash`。配置后重启 `start_server.bat`。

处理顺序为：本地规则兜底 → DeepSeek 文字元数据判断 → Qwen 缩略图视觉复审。只有通过文字初筛且存在缩略图的候选才产生视觉调用；任一模型超时或报错时，任务会保留降级判断并继续执行。单张缩略图无法证明瞬时断裂事件，因此视觉证据不足时结果会保持“待审核”。

在审核台点击“立即运行”后等待搜索完成。勾选候选后可以直接点“下载选中”，系统会自动标记保留并加入下载队列。默认保存到项目的 `downloads/<主题>/`。也可以在审核台输入路径或点击“选择文件夹”，下载时会在所选目录下继续按主题建立子文件夹。

下载开始后，候选行会显示实时进度条、百分比以及已下载/总大小，并每秒自动刷新；下载完成后显示 100%。

下载完成的候选会显示“打开文件夹”按钮，点击即可在 Windows 资源管理器中打开该视频所在目录。

### 高相关自动筛选与视觉 RAG

审核台不再显示“标记保留/标记排除”。每次运行会扩大原始搜索池，只把全新且判定为“高相关”的视频加入结果，目标上限为 100 条；公开来源不足时会显示实际数量，不会伪造结果或无限请求。

从 Agent 页点击“视觉 RAG 知识库”进入独立的 `/rag` 管理模块。该模块支持一次选择多个视频批量上传、查看样本与索引信息、修改名称以及单条删除。批量处理中每个文件独立建立索引，单个失败不会影响其他文件。单个上传视频不超过 250 MB；系统用 ffmpeg 抽取最多 12 张关键帧并建立本地视觉指纹。样本库非空后，新候选除了必须文字高相关，还必须与样本关键帧达到 70% 的视觉相似度。样本与索引保存在被 Git 忽略的 `agent_data/` 下。

目前关键词发现源仍是 YouTube。TikTok、Instagram、X、抖音和微信视频号的“已知公开视频链接解析”与“全站关键词发现”不是同一能力；全站发现必须使用平台允许的官方 API 或合法授权数据，系统不会绕过登录、验证码或反爬限制。

勾选后点击“删除候选”会把记录从默认审核列表隐藏，并阻止它进入下载队列。这是可追溯的软删除，只修改 SQLite 审核状态，不会删除已经下载的视频文件。长期状态保存在 `agent_data/paqu_agent.sqlite3`，同时导出带 UTF-8 BOM 的 `agent_data/candidates.csv` 供 Excel 查看。

命令行手动执行：

```powershell
.\run_daily_agent.bat
```

同一天已经成功运行时会安全跳过。需要调试重跑可使用：

```powershell
py -3 agent_core.py --limit 100 --window-days 30 --force
```

安装每天 09:00 的 Windows 任务（错过时间后在下次可用时补跑）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_daily_task.ps1
```

任务名为 `paqu 每日视频发现 Agent`。安装脚本会输出任务状态供核对。电脑需开机且当前用户已登录；Agent 不会绕过登录、验证码、付费墙、DRM 或访问控制。

启动窗口需要保持打开，关闭后网页服务会停止。

启动本地网页工具：

```powershell
python web_app.py
```

如果 `python` 命令不可用，可以使用 Windows Python 启动器：

```powershell
py -3 web_app.py
```

然后打开：

```text
http://127.0.0.1:8080
```

网页支持输入页面链接，选择要抓取的内容类型：视频、图片、文字。视频可选择清晰度，并下载到项目内 `downloads/` 或你填写的保存目录；图片和文字可在结果表里复制。
下载时优先由 `yt-dlp` 合并为 `.mp4`；备用直链/HLS 下载完成后会用 `ffmpeg` 尽量转为 `.mp4`。项目依赖里的 `imageio-ffmpeg` 会提供可用的 ffmpeg。
扫描视频时会先让 `yt-dlp` 识别页面；如果 `yt-dlp` 没有识别出可下载格式，才会进入受 `robots.txt` 约束的 Scrapling 备用页面抓取。扫描图片和文字时直接使用 Scrapling，并同样遵守 `robots.txt`。

### YouTube 提示需要登录或验证时

如果 YouTube 返回 `Sign in to confirm you're not a bot`，说明 YouTube 当前要求带登录态访问。先用浏览器扩展导出 Netscape 格式的 `cookies.txt`，再在页面里的 `Cookies 文件` 输入框填入该文件路径，例如：

```text
E:\vibe-coding\paqu\cookies.txt
```

扫描和下载都会把这个 cookies 文件交给 `yt-dlp`。工具不会自动读取浏览器登录态，也不会绕过验证码、付费墙、DRM 或私有内容访问控制。

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

如果 `python` 命令不可用，可以使用 Windows Python 启动器：

```powershell
py -3 video_crawler.py "https://example.com/page"
```

## 支持范围

- 图片：`<img src>`、`srcset`、常见懒加载属性、`og:image` / `twitter:image`
- 文字：页面标题、`h1` / `h2` 标题、段落文本
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
