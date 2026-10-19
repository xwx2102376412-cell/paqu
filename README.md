# paqu 单链接视频爬取工具

本项目用于抓取你有权限访问的网页中的公开视频、图片和文字内容。不会绕过登录、付费墙、DRM、验证码或其他访问控制，并应遵守目标网站的服务条款、`robots.txt` 和速率限制。

## 启动网页工具

```powershell
.\start_server.bat
```

或：

```powershell
py -3 web_app.py
```

打开 `http://127.0.0.1:8080/`，输入一个页面链接即可扫描视频、图片或文字，并按需要下载视频。视觉 RAG 知识库位于 `http://127.0.0.1:8080/rag`。

## 命令行单链接入口

扫描一个页面：

```powershell
py -3 video_crawler.py "https://example.com/page"
```

输出 JSON：

```powershell
py -3 video_crawler.py "https://example.com/page" --json
```

扫描并下载：

```powershell
py -3 video_crawler.py "https://example.com/page" --download --output-dir downloads
```

项目已移除每日视频发现 Agent、批量关键词试跑和 Windows 每日定时任务；不会自动搜索或处理视频。已有下载文件不会被删除。

## 依赖

```powershell
py -3 -m pip install -r requirements.txt
```
