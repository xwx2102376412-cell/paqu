#!/usr/bin/env python3
"""
Local web UI for paqu video crawler.

Run:
    python web_app.py
Then open:
    http://127.0.0.1:8080
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from video_crawler import (
    DEFAULT_USER_AGENT,
    DownloadCancelled,
    VideoQuality,
    can_fetch,
    discover_scrapling_resources,
    discover_ytdlp_resources,
    convert_to_mp4,
    download_file,
    download_hls_playlist,
    ytdlp_download,
)


HOST = "127.0.0.1"
PORT = 8080
DOWNLOAD_DIR = Path("downloads")
REQUEST_TIMEOUT = 20.0


@dataclass
class DownloadTask:
    id: str
    url: str
    quality_label: str
    kind: str
    format_id: str | None
    output_dir: str
    status: str = "queued"
    cancel_requested: bool = False
    progress: int = 0
    total: int = 0
    saved_to: str | None = None
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


TASKS: dict[str, DownloadTask] = {}
TASKS_LOCK = threading.Lock()


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>paqu 视频爬取工具</title>
  <style>
    :root {
      --bg: #f7f9fc;
      --panel: #ffffff;
      --panel-soft: #f2f5f9;
      --border: #d9e1ea;
      --text: #172033;
      --muted: #687386;
      --accent: #2368d1;
      --accent-dark: #194f9f;
      --success: #138a5b;
      --warning: #b7791f;
      --danger: #bd2f2f;
      --shadow: 0 18px 48px rgba(23, 32, 51, 0.08);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    .shell {
      width: min(1360px, calc(100% - 40px));
      margin: 0 auto;
      padding: 24px 0 32px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 22px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .brand-mark {
      width: 34px;
      height: 34px;
      border-radius: 8px;
      background: linear-gradient(135deg, #2368d1, #41a4ff);
      box-shadow: 0 10px 24px rgba(35, 104, 209, 0.24);
    }

    h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.15;
      font-weight: 760;
    }

    .note {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .command {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(260px, 360px) auto;
      gap: 12px;
      padding: 14px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }

    input, select, button {
      font: inherit;
    }

    input, select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 7px;
      background: #fff;
      color: var(--text);
      min-height: 42px;
      padding: 0 12px;
      font-size: 14px;
      outline: none;
    }

    input:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(35, 104, 209, 0.14);
    }

    button {
      border: 0;
      border-radius: 7px;
      min-height: 42px;
      padding: 0 16px;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
    }

    button:hover { background: var(--accent-dark); }
    button:disabled { opacity: 0.52; cursor: not-allowed; }

    button.secondary {
      width: 100%;
      min-height: 34px;
      margin-top: 10px;
      border: 1px solid var(--border);
      background: #fff;
      color: var(--text);
      font-size: 13px;
    }

    button.secondary:hover {
      background: var(--panel-soft);
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 18px;
      align-items: start;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }

    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 15px 16px;
      border-bottom: 1px solid var(--border);
      background: #fff;
    }

    h2 {
      margin: 0;
      font-size: 15px;
      line-height: 1.3;
      font-weight: 760;
    }

    .status {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }

    .field {
      display: grid;
      gap: 6px;
    }

    .field label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    .table-wrap { overflow-x: auto; }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 800px;
    }

    th, td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
      text-align: left;
      font-size: 13px;
    }

    th {
      color: var(--muted);
      font-weight: 700;
      background: var(--panel-soft);
    }

    .url-cell {
      max-width: 360px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: #25324a;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      background: #eaf1ff;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .empty {
      padding: 42px 18px;
      text-align: center;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
    }

    .queue {
      display: grid;
      gap: 10px;
      padding: 14px;
    }

    .task {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
    }

    .task-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 7px;
      font-size: 13px;
      font-weight: 740;
    }

    .task-meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .bar {
      height: 7px;
      margin: 9px 0;
      overflow: hidden;
      border-radius: 99px;
      background: var(--panel-soft);
    }

    .bar > span {
      display: block;
      height: 100%;
      width: 0%;
      background: var(--success);
      transition: width 180ms ease;
    }

    .error { color: var(--danger); }
    .success { color: var(--success); }
    .warning { color: var(--warning); }

    footer {
      margin-top: 16px;
      color: var(--muted);
      font-size: 13px;
    }

    @media (max-width: 900px) {
      .shell { width: min(100% - 24px, 760px); padding-top: 16px; }
      header { align-items: flex-start; flex-direction: column; gap: 8px; }
      .command { grid-template-columns: 1fr; }
      .layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"></div>
        <h1>paqu 视频爬取工具</h1>
      </div>
      <div class="note">优先使用 yt-dlp；请只下载你有权限保存的内容。</div>
    </header>

    <form class="command" id="scanForm">
      <div class="field">
        <label for="urlInput">页面链接</label>
        <input id="urlInput" name="url" type="url" placeholder="粘贴网页链接" required />
      </div>
      <div class="field">
        <label for="outputDirInput">保存到</label>
        <input id="outputDirInput" name="outputDir" type="text" placeholder="默认：downloads" />
      </div>
      <button id="scanButton" type="submit">扫描页面</button>
    </form>

    <section class="layout">
      <section class="panel" aria-labelledby="resultsTitle">
        <div class="panel-head">
          <h2 id="resultsTitle">发现的视频</h2>
          <div class="status" id="scanStatus">待扫描</div>
        </div>
        <div class="table-wrap" id="results"></div>
      </section>

      <aside class="panel" aria-labelledby="queueTitle">
        <div class="panel-head">
          <h2 id="queueTitle">下载队列</h2>
          <div class="status" id="queueCount">0 个任务</div>
        </div>
        <div class="queue" id="queue">
          <div class="empty">选择清晰度并点击“下载 MP4”后，任务会显示在这里。</div>
        </div>
      </aside>
    </section>

    <footer>工具优先使用 yt-dlp 识别页面可下载格式并合并为 MP4；yt-dlp 不支持时回退到公开视频直链和未加密 HLS。</footer>
  </main>

  <script>
    const scanForm = document.querySelector("#scanForm");
    const urlInput = document.querySelector("#urlInput");
    const outputDirInput = document.querySelector("#outputDirInput");
    const scanButton = document.querySelector("#scanButton");
    const scanStatus = document.querySelector("#scanStatus");
    const results = document.querySelector("#results");
    const queue = document.querySelector("#queue");
    const queueCount = document.querySelector("#queueCount");
    const tasks = new Map();

    function htmlEscape(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
      }[char]));
    }

    function qualityOptions(resource, index) {
      return resource.qualities.map((quality, qIndex) => {
        const disabled = quality.supported ? "" : " disabled";
        const detail = quality.reason ? ` - ${quality.reason}` : "";
        return `<option value="${qIndex}"${disabled}>${htmlEscape(localizeQualityLabel(quality.label))}${htmlEscape(detail)}</option>`;
      }).join("");
    }

    function localizeQualityLabel(label) {
      return label === "original" ? "原始清晰度" : label;
    }

    function renderResults(resources) {
      if (!resources.length) {
        results.innerHTML = `<div class="empty">没有在页面 HTML 中发现公开视频链接。</div>`;
        return;
      }

      const rows = resources.map((resource, index) => {
        const hasSupportedQuality = resource.qualities.some(quality => quality.supported);
        return `
        <tr>
          <td><span class="pill">${htmlEscape(resource.kind)}</span></td>
          <td class="url-cell" title="${htmlEscape(resource.url)}">${htmlEscape(resource.url)}</td>
          <td>${htmlEscape(resource.source)}</td>
          <td>
            <select data-quality="${index}" aria-label="清晰度">
              ${qualityOptions(resource, index)}
            </select>
          </td>
          <td>
            <button type="button" data-download="${index}"${hasSupportedQuality ? "" : " disabled"}>${hasSupportedQuality ? "下载 MP4" : "不可下载"}</button>
          </td>
        </tr>
        `;
      }).join("");

      results.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>类型</th>
              <th>链接</th>
              <th>来源</th>
              <th>清晰度</th>
              <th>下载</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;

      results.querySelectorAll("[data-download]").forEach(button => {
        button.addEventListener("click", () => {
          const index = Number(button.dataset.download);
          const select = results.querySelector(`[data-quality="${index}"]`);
          const quality = resources[index].qualities[Number(select.value)];
          startDownload(quality);
        });
      });
    }

    function renderQueue() {
      queueCount.textContent = `${tasks.size} 个任务`;
      if (!tasks.size) {
        queue.innerHTML = `<div class="empty">选择清晰度并点击“下载 MP4”后，任务会显示在这里。</div>`;
        return;
      }

      queue.innerHTML = Array.from(tasks.values()).reverse().map(task => {
        const pct = task.total ? Math.round((task.progress / task.total) * 100) : (task.status === "done" ? 100 : 0);
        const statusClass = task.status === "done" ? "success" : task.status === "failed" ? "error" : task.status === "queued" || task.status === "converting" || task.status === "cancelled" ? "warning" : "";
        return `
          <article class="task">
            <div class="task-title">
              <span>${htmlEscape(localizeQualityLabel(task.quality_label))}</span>
              <span class="${statusClass}">${htmlEscape(localizeStatus(task.status))}</span>
            </div>
            <div class="bar"><span style="width:${pct}%"></span></div>
            <div class="task-meta">${pct}% · ${htmlEscape(task.url)}</div>
            <div class="task-meta">保存目录：${htmlEscape(task.output_dir)}</div>
            ${task.saved_to ? `<div class="task-meta success">已保存到 ${htmlEscape(task.saved_to)}</div>` : ""}
            ${task.error ? `<div class="task-meta error">${htmlEscape(task.error)}</div>` : ""}
            ${task.saved_to ? `<button class="secondary" type="button" data-open-folder="${task.id}">打开文件夹</button>` : ""}
            ${canCancel(task) ? `<button class="secondary" type="button" data-cancel="${task.id}">取消下载</button>` : ""}
          </article>
        `;
      }).join("");

      queue.querySelectorAll("[data-open-folder]").forEach(button => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await postJson(`/api/tasks/${button.dataset.openFolder}/open-folder`, {});
          } finally {
            button.disabled = false;
          }
        });
      });

      queue.querySelectorAll("[data-cancel]").forEach(button => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          const task = await postJson(`/api/tasks/${button.dataset.cancel}/cancel`, {});
          tasks.set(task.id, task);
          renderQueue();
        });
      });
    }

    function canCancel(task) {
      return task.status === "queued" || task.status === "running" || task.status === "converting";
    }

    function localizeStatus(status) {
      return {
        queued: "排队中",
        running: "下载中",
        done: "已完成",
        converting: "转换中",
        cancelled: "已取消",
        failed: "失败"
      }[status] || status;
    }

    async function postJson(path, body) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Request failed");
      return data;
    }

    async function startDownload(quality) {
      const task = await postJson("/api/download", {
        ...quality,
        output_dir: outputDirInput.value.trim()
      });
      tasks.set(task.id, task);
      renderQueue();
      pollTask(task.id);
    }

    async function pollTask(id) {
      const response = await fetch(`/api/tasks/${id}`);
      const task = await response.json();
      tasks.set(id, task);
      renderQueue();
      if (task.status === "queued" || task.status === "running" || task.status === "converting") {
        window.setTimeout(() => pollTask(id), 800);
      }
    }

    scanForm.addEventListener("submit", async event => {
      event.preventDefault();
      scanButton.disabled = true;
      scanStatus.textContent = "扫描中...";
      results.innerHTML = `<div class="empty">正在扫描页面并检查公开视频链接...</div>`;

      try {
        const data = await postJson("/api/scan", { url: urlInput.value.trim() });
        renderResults(data.resources);
        scanStatus.textContent = `发现 ${data.resources.length} 个`;
      } catch (error) {
        results.innerHTML = `<div class="empty error">${htmlEscape(error.message)}</div>`;
        scanStatus.textContent = "扫描失败";
      } finally {
        scanButton.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def scan_page_resources(url: str) -> tuple[HTTPStatus, dict[str, Any]]:
    ytdlp_resources = discover_ytdlp_resources(url, REQUEST_TIMEOUT)
    if ytdlp_resources:
        return HTTPStatus.OK, {"resources": [asdict(resource) for resource in ytdlp_resources]}

    if not can_fetch(url, DEFAULT_USER_AGENT):
        return HTTPStatus.FORBIDDEN, {"error": "yt-dlp 未能识别该链接，且 robots.txt 不允许使用备用页面抓取。"}

    resources = discover_scrapling_resources(url, DEFAULT_USER_AGENT, REQUEST_TIMEOUT)
    return HTTPStatus.OK, {"resources": [asdict(resource) for resource in resources]}


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def serialize_task(task: DownloadTask) -> dict[str, Any]:
    return asdict(task)


def resolve_output_dir(value: str | None) -> Path:
    raw_value = (value or "").strip()
    output_dir = Path(raw_value) if raw_value else DOWNLOAD_DIR
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("保存位置必须是文件夹，不能是文件。")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def create_task(quality: VideoQuality, output_dir: Path | None = None) -> DownloadTask:
    now = time.time()
    resolved_output_dir = output_dir or resolve_output_dir(None)
    task = DownloadTask(
        id=uuid4().hex,
        url=quality.url,
        quality_label=quality.label,
        kind=quality.kind,
        format_id=quality.format_id,
        output_dir=str(resolved_output_dir),
        created_at=now,
        updated_at=now,
    )
    with TASKS_LOCK:
        TASKS[task.id] = task
    thread = threading.Thread(target=run_download_task, args=(task.id,), daemon=True)
    thread.start()
    return task


def update_task(task_id: str, **changes: Any) -> None:
    with TASKS_LOCK:
        task = TASKS[task_id]
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()


def update_task_progress(task_id: str, done: int, total: int) -> None:
    if total <= 0:
        return
    with TASKS_LOCK:
        task = TASKS[task_id]
        current_ratio = (task.progress / task.total) if task.total else 0
        next_ratio = done / total
        if next_ratio >= current_ratio:
            task.progress = done
            task.total = total
            task.updated_at = time.time()


def finish_task_progress(task_id: str) -> None:
    with TASKS_LOCK:
        task = TASKS[task_id]
        if task.total:
            task.progress = task.total
        else:
            task.progress = 1
            task.total = 1
        task.updated_at = time.time()


def is_task_cancelled(task_id: str) -> bool:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        return bool(task and task.cancel_requested)


def cancel_task(task_id: str) -> DownloadTask | None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        if task.status in {"done", "failed", "cancelled"}:
            return task
        task.cancel_requested = True
        task.status = "cancelled" if task.status == "queued" else task.status
        task.updated_at = time.time()
        return task


def folder_for_task(task: DownloadTask) -> Path:
    if task.saved_to:
        saved_path = Path(task.saved_to)
        return saved_path.parent if saved_path.suffix else saved_path
    return Path(task.output_dir)


def open_folder(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        raise ValueError("文件夹不存在。")
    subprocess.Popen(["explorer", str(path)])


def run_download_task(task_id: str) -> None:
    with TASKS_LOCK:
        task = TASKS[task_id]
        url = task.url
        kind = task.kind
        format_id = task.format_id
        output_dir = Path(task.output_dir)

    update_task(task_id, status="running")
    try:
        if is_task_cancelled(task_id):
            raise DownloadCancelled("下载已取消。")
        if kind == "ytdlp":
            saved_to = ytdlp_download(
                url,
                output_dir,
                format_id,
                REQUEST_TIMEOUT,
                should_cancel=lambda: is_task_cancelled(task_id),
                progress=lambda done, total: update_task_progress(task_id, done, total),
            )
            finish_task_progress(task_id)
        else:
            if kind == "hls":
                downloaded_to = download_hls_playlist(
                    url,
                    output_dir,
                    DEFAULT_USER_AGENT,
                    REQUEST_TIMEOUT,
                    progress=lambda done, total: update_task_progress(task_id, done, total),
                    should_cancel=lambda: is_task_cancelled(task_id),
                )
            else:
                if is_task_cancelled(task_id):
                    raise DownloadCancelled("下载已取消。")
                downloaded_to = download_file(
                    url,
                    output_dir,
                    DEFAULT_USER_AGENT,
                    REQUEST_TIMEOUT,
                    progress=lambda done, total: update_task_progress(task_id, done, total),
                    should_cancel=lambda: is_task_cancelled(task_id),
                )
                finish_task_progress(task_id)
                if is_task_cancelled(task_id):
                    raise DownloadCancelled("下载已取消。")
            update_task(task_id, status="converting")
            if is_task_cancelled(task_id):
                raise DownloadCancelled("下载已取消。")
            saved_to = convert_to_mp4(downloaded_to)
            if is_task_cancelled(task_id):
                raise DownloadCancelled("下载已取消。")
        finish_task_progress(task_id)
        update_task(task_id, status="done", saved_to=str(saved_to))
    except DownloadCancelled as exc:
        update_task(task_id, status="cancelled", error=str(exc))
    except Exception as exc:
        update_task(task_id, status="failed", error=str(exc))


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path.startswith("/api/tasks/"):
            task_id = parsed.path.rsplit("/", 1)[-1]
            with TASKS_LOCK:
                task = TASKS.get(task_id)
                payload = serialize_task(task) if task else None
            if not payload:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "Task not found"})
                return
            json_response(self, HTTPStatus.OK, payload)
            return

        json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/api/scan":
                self.handle_scan()
                return
            if self.path == "/api/download":
                self.handle_download()
                return
            if self.path.startswith("/api/tasks/") and self.path.endswith("/cancel"):
                self.handle_cancel()
                return
            if self.path.startswith("/api/tasks/") and self.path.endswith("/open-folder"):
                self.handle_open_folder()
                return
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except json.JSONDecodeError:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_scan(self) -> None:
        payload = read_json(self)
        url = str(payload.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "请输入 http:// 或 https:// 开头的链接。"})
            return

        status, response = scan_page_resources(url)
        json_response(self, status, response)

    def handle_download(self) -> None:
        payload = read_json(self)
        quality = VideoQuality(
            label=str(payload.get("label", "original")),
            url=str(payload.get("url", "")),
            kind=str(payload.get("kind", "direct")),
            format_id=payload.get("format_id"),
            supported=bool(payload.get("supported", True)),
            reason=payload.get("reason"),
        )
        if not quality.url.startswith(("http://", "https://")):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "下载链接必须以 http:// 或 https:// 开头。"})
            return
        if not quality.supported:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": quality.reason or "暂不支持这个视频流。"})
            return

        try:
            output_dir = resolve_output_dir(payload.get("output_dir"))
        except OSError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"无法创建保存目录：{exc}"})
            return
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        task = create_task(quality, output_dir)
        json_response(self, HTTPStatus.OK, serialize_task(task))

    def handle_cancel(self) -> None:
        task_id = self.path.split("/")[-2]
        task = cancel_task(task_id)
        if not task:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "任务不存在。"})
            return
        json_response(self, HTTPStatus.OK, serialize_task(task))

    def handle_open_folder(self) -> None:
        task_id = self.path.split("/")[-2]
        with TASKS_LOCK:
            task = TASKS.get(task_id)
        if not task:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "任务不存在。"})
            return
        try:
            folder = folder_for_task(task)
            open_folder(folder)
        except (OSError, ValueError) as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(self, HTTPStatus.OK, {"ok": True, "folder": str(folder)})


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"paqu video crawler running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
