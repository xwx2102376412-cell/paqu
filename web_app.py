#!/usr/bin/env python3
"""
Local web UI for paqu video crawler.

Run:
    python web_app.py
Then open:
    http://127.0.0.1:8080
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
import tkinter
from tkinter import filedialog
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_core import AgentStore, download_candidates, model_status, run_discovery, topic_payload
from video_crawler import (
    DEFAULT_USER_AGENT,
    DownloadCancelled,
    VideoQuality,
    can_fetch,
    discover_video_resources,
    discover_scrapling_content,
    discover_weixin_finder_resources,
    discover_ytdlp,
    weixin_finder_id,
    convert_to_mp4,
    download_file,
    download_hls_playlist,
    normalize_content_types,
    ytdlp_download,
)
from visual_rag import extract_video_hashes


HOST = "127.0.0.1"
PORT = int(os.environ.get("PAQU_PORT", "8080"))
DOWNLOAD_DIR = Path("downloads")
RAG_UPLOAD_DIR = Path("agent_data") / "rag_uploads"
REQUEST_TIMEOUT = 20.0
YOUTUBE_COOKIE_FILENAMES = ("www.youtube.com_cookies.txt", "youtube.com_cookies.txt", "cookies.txt")
DOUYIN_COOKIE_FILENAMES = ("www.douyin.com_cookies.txt", "douyin.com_cookies.txt")
FACEBOOK_COOKIE_FILENAMES = ("www.facebook.com_cookies.txt", "facebook.com_cookies.txt")
TIKTOK_COOKIE_FILENAMES = ("www.tiktok.com_cookies.txt", "tiktok.com_cookies.txt")
DEFAULT_YOUTUBE_COOKIES_FILE = "www.youtube.com_cookies.txt"
DEFAULT_DOUYIN_COOKIES_FILE = "www.douyin.com_cookies.txt"
DEFAULT_FACEBOOK_COOKIES_FILE = "www.facebook.com_cookies.txt"
DEFAULT_TIKTOK_COOKIES_FILE = "www.tiktok.com_cookies.txt"


@dataclass
class DownloadTask:
    id: str
    url: str
    quality_label: str
    kind: str
    format_id: str | None
    output_dir: str
    cookies_file: str | None = None
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
AGENT_STORE: AgentStore | None = None
AGENT_RUN_LOCK = threading.Lock()
AGENT_RUN_STATE: dict[str, Any] = {"status": "idle"}


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>paqu 内容爬取工具</title>
  <style>
    :root {
      --bg: #edf2f0;
      --panel: #ffffff;
      --panel-soft: #f6f8f7;
      --panel-strong: #e7ecea;
      --border: #cfd9d5;
      --border-soft: #e0e7e4;
      --text: #14201c;
      --muted: #63716d;
      --accent: #155eef;
      --accent-dark: #0f46bd;
      --success: #138a5b;
      --warning: #a96f16;
      --danger: #b73737;
      --shadow: 0 18px 46px rgba(21, 42, 35, 0.12);
      --radius: 8px;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(rgba(20, 32, 28, 0.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(20, 32, 28, 0.035) 1px, transparent 1px),
        var(--bg);
      background-size: 28px 28px, 28px 28px, auto;
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0 0 auto;
      height: 220px;
      pointer-events: none;
      background:
        linear-gradient(115deg, rgba(21, 94, 239, 0.14), rgba(19, 138, 91, 0.08) 44%, transparent 72%),
        linear-gradient(180deg, #f8fbfa 0%, rgba(248, 251, 250, 0) 100%);
    }

    .shell {
      width: min(1360px, calc(100% - 40px));
      margin: 0 auto;
      padding: 22px 0 34px;
      position: relative;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 16px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .brand-copy {
      display: grid;
      gap: 2px;
    }

    .brand-kicker {
      color: var(--muted);
      font-size: 11px;
      font-weight: 740;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .brand-mark {
      width: 38px;
      height: 38px;
      border-radius: 8px;
      background:
        linear-gradient(135deg, #155eef, #138a5b);
      box-shadow: 0 12px 24px rgba(21, 94, 239, 0.24);
      position: relative;
    }

    .brand-mark::after {
      content: "";
      position: absolute;
      inset: 10px;
      border: 2px solid rgba(255, 255, 255, 0.82);
      border-left-color: transparent;
      border-radius: 50%;
    }

    h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.15;
      font-weight: 760;
    }

    .note {
      color: #53645f;
      font-size: 12px;
      line-height: 1.5;
      padding: 7px 10px;
      border: 1px solid var(--border-soft);
      border-radius: var(--radius);
      background: rgba(255, 255, 255, 0.78);
    }

    .command {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(190px, 260px) minmax(190px, 260px) minmax(220px, 270px) 132px;
      gap: 12px;
      padding: 16px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(250, 252, 251, 0.96));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }

    .path-control { display: grid; grid-template-columns: 1fr auto; gap: 7px; }
    .path-control button { min-height: 44px; padding: 0 13px; box-shadow: none; white-space: nowrap; }

    input, select, button {
      font: inherit;
    }

    input, select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 7px;
      background: #fff;
      color: var(--text);
      min-height: 44px;
      padding: 0 12px;
      font-size: 14px;
      outline: none;
    }

    input:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(21, 94, 239, 0.14);
    }

    button {
      border: 0;
      border-radius: 7px;
      min-height: 44px;
      padding: 0 16px;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 10px 18px rgba(21, 94, 239, 0.18);
    }

    button:hover { background: var(--accent-dark); }
    button:active { transform: translateY(1px); }
    button:disabled {
      opacity: 0.52;
      cursor: not-allowed;
      box-shadow: none;
      transform: none;
    }

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

    .command > button {
      align-self: end;
      width: 100%;
    }

    .mode-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin: -4px 0 18px;
    }

    .mode-item {
      min-height: 52px;
      padding: 10px 12px;
      border: 1px solid var(--border-soft);
      border-left: 3px solid var(--accent);
      border-radius: var(--radius);
      background: rgba(255, 255, 255, 0.82);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .mode-item strong {
      display: block;
      margin-bottom: 2px;
      color: var(--text);
      font-size: 13px;
    }

    .mode-item code {
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 720;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 18px;
      align-items: start;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      box-shadow: 0 12px 32px rgba(21, 42, 35, 0.07);
    }

    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 15px 16px;
      border-bottom: 1px solid var(--border);
      background:
        linear-gradient(180deg, #fff, var(--panel-soft));
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
      white-space: nowrap;
    }

    .field {
      display: grid;
      gap: 6px;
    }

    .field label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.01em;
    }

    .checks {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 42px;
      flex-wrap: wrap;
    }

    .check {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 30px;
      padding: 0 8px;
      border: 1px solid var(--border-soft);
      border-radius: 7px;
      background: var(--panel-soft);
      color: var(--text);
      font-size: 13px;
      font-weight: 650;
    }

    .check input {
      width: 16px;
      height: 16px;
      min-height: 16px;
      padding: 0;
      accent-color: var(--accent);
    }

    .check:has(input:checked) {
      border-color: rgba(37, 111, 210, 0.36);
      background: #edf4ff;
      color: var(--accent-dark);
    }

    .table-wrap { overflow-x: auto; }

    .result-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      background: #f8faf9;
    }

    .selection-summary { color: var(--muted); font-size: 13px; }
    .toolbar-actions { display: flex; gap: 8px; }
    .toolbar-actions button { min-height: 36px; padding: 0 13px; box-shadow: none; }
    .toolbar-actions .secondary { width: auto; min-height: 36px; margin: 0; }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 780px;
      table-layout: fixed;
    }

    th:nth-child(1), td:nth-child(1) { width: 48px; text-align: center; }
    th:nth-child(2), td:nth-child(2) { width: 82px; }
    th:nth-child(5), td:nth-child(5) { width: 150px; }
    th:nth-child(6), td:nth-child(6) { width: 104px; }

    th, td {
      padding: 13px 14px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
      text-align: left;
      font-size: 13px;
    }

    th {
      color: var(--muted);
      font-weight: 700;
      background: var(--panel-strong);
    }

    tbody tr:hover {
      background: #f8fbfe;
    }

    tbody tr.is-selected { background: #edf4ff; }
    .row-check, .select-all { width: 17px; height: 17px; min-height: 17px; accent-color: var(--accent); }
    .thumb { width: 54px; height: 54px; object-fit: cover; border-radius: 7px; background: var(--panel-strong); display: block; }
    .thumb-fallback { display: grid; place-items: center; color: var(--muted); font-size: 11px; }

    .url-cell {
      overflow-wrap: anywhere;
      color: #21332d;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 12px;
    }

    .source-cell {
      overflow-wrap: anywhere;
    }

    .detail-cell {
      color: var(--muted);
      overflow-wrap: anywhere;
      line-height: 1.45;
    }

    .detail-cell select {
      min-width: 0;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      background: #edf4ff;
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
      max-height: calc(100vh - 238px);
      overflow: auto;
    }

    .task {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 12px;
      background: linear-gradient(180deg, #fff, #f7faf8);
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
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
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
      background: linear-gradient(90deg, var(--accent), var(--success));
      transition: width 180ms ease;
    }

    .error { color: var(--danger); }
    .success { color: var(--success); }
    .warning { color: var(--warning); }

    .disabled-reason {
      color: var(--warning);
      font-weight: 650;
    }

    footer {
      margin-top: 16px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }

    @media (max-width: 900px) {
      .shell { width: min(100% - 24px, 760px); padding-top: 16px; }
      header { align-items: flex-start; flex-direction: column; gap: 8px; }
      .command { grid-template-columns: 1fr; }
      .mode-strip { grid-template-columns: 1fr; }
      .layout { grid-template-columns: 1fr; }
      .queue { max-height: none; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"></div>
        <div class="brand-copy">
          <div class="brand-kicker">Crawler Workbench</div>
          <h1>paqu 内容爬取工具</h1>
        </div>
      </div>
      <div class="note"><a href="/agent">打开每日视频 Agent</a> · 本地运行 · 公开视频资源</div>
    </header>

    <form class="command" id="scanForm">
      <div class="field">
        <label for="urlInput">页面链接</label>
        <input id="urlInput" name="url" type="url" placeholder="粘贴网页链接" required />
      </div>
      <div class="field">
        <label for="outputDirInput">保存到</label>
        <div class="path-control">
          <input id="outputDirInput" name="outputDir" type="text" placeholder="默认：downloads" />
          <button id="selectFolderButton" type="button">选择</button>
        </div>
      </div>
      <div class="field">
        <label for="cookiesFileInput">Cookies 文件</label>
        <input id="cookiesFileInput" name="cookiesFile" type="text" placeholder="自动尝试当前网站的 cookies.txt" />
        <input id="cookiesImportInput" type="file" accept=".txt" hidden />
        <button id="importCookiesButton" type="button" class="secondary">导入当前网站 cookies</button>
      </div>
      <div class="field">
        <label>抓取内容</label>
        <div class="checks" id="contentTypeInputs">
          <label class="check"><input type="checkbox" value="video" checked />视频</label>
          <label class="check"><input type="checkbox" value="image" />图片</label>
          <label class="check"><input type="checkbox" value="text" />文字</label>
        </div>
      </div>
      <button id="scanButton" type="submit">扫描页面</button>
    </form>

    <section class="mode-strip" aria-label="抓取能力">
      <div class="mode-item">
        <strong>Primary Engine</strong>
        <code>yt-dlp</code> 视频解析
      </div>
      <div class="mode-item">
        <strong>Finder Rule</strong>
        <code>weixin.sph</code> 专用识别
      </div>
      <div class="mode-item">
        <strong>Fallback Scan</strong>
        <code>Scrapling</code> 图片与文字
      </div>
    </section>

    <section class="layout">
      <section class="panel" aria-labelledby="resultsTitle">
        <div class="panel-head">
          <h2 id="resultsTitle">发现的内容</h2>
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

    <footer>工具会遵守 robots.txt；视频可下载为 MP4，图片和文字会展示在扫描结果中，方便复制链接或内容。</footer>
  </main>

  <script>
    const scanForm = document.querySelector("#scanForm");
    const urlInput = document.querySelector("#urlInput");
    const outputDirInput = document.querySelector("#outputDirInput");
    const selectFolderButton = document.querySelector("#selectFolderButton");
    const cookiesFileInput = document.querySelector("#cookiesFileInput");
    const cookiesImportInput = document.querySelector("#cookiesImportInput");
    const importCookiesButton = document.querySelector("#importCookiesButton");
    const scanButton = document.querySelector("#scanButton");
    const scanStatus = document.querySelector("#scanStatus");
    const results = document.querySelector("#results");
    const queue = document.querySelector("#queue");
    const queueCount = document.querySelector("#queueCount");
    const contentTypeInputs = document.querySelector("#contentTypeInputs");
    const tasks = new Map();
    let currentResources = [];
    const selectedResources = new Set();

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

    function selectedContentTypes() {
      return Array.from(contentTypeInputs.querySelectorAll("input:checked")).map(input => input.value);
    }

    function localizeResourceType(resource) {
      return {
        video: "视频",
        image: "图片",
        text: "文字"
      }[resource.content_type] || resource.content_type || resource.kind;
    }

    function resourceValue(resource) {
      if (resource.content_type === "text") return resource.text || resource.preview || "";
      return resource.url || "";
    }

    async function copyValue(value) {
      await navigator.clipboard.writeText(value);
    }

    function firstUnsupportedReason(resource) {
      const quality = (resource.qualities || []).find(item => !item.supported && item.reason);
      return quality ? quality.reason : "没有发现可下载的视频地址";
    }

    function resourceDetail(resource, isVideo, hasSupportedQuality, index) {
      if (!isVideo) return htmlEscape(resource.kind);
      if (hasSupportedQuality) {
        return `<select data-quality="${index}" aria-label="清晰度">${qualityOptions(resource, index)}</select>`;
      }
      return `<span class="disabled-reason">${htmlEscape(firstUnsupportedReason(resource))}</span>`;
    }

    function renderResults(resources) {
      currentResources = resources;
      selectedResources.clear();
      if (!resources.length) {
        results.innerHTML = `<div class="empty">没有发现匹配的公开内容。</div>`;
        return;
      }

      const rows = resources.map((resource, index) => {
        const isVideo = resource.content_type === "video" || !resource.content_type;
        const hasSupportedQuality = isVideo && resource.qualities.some(quality => quality.supported);
        const value = resourceValue(resource);
        const preview = resource.preview ? ` title="${htmlEscape(resource.preview)}"` : "";
        const selectable = isVideo && hasSupportedQuality;
        const thumbnail = resource.preview && /^https?:\/\//i.test(resource.preview)
          ? `<img class="thumb" src="${htmlEscape(resource.preview)}" alt="视频封面" loading="lazy" />`
          : `<div class="thumb thumb-fallback">${isVideo ? "视频" : htmlEscape(localizeResourceType(resource))}</div>`;
        return `
        <tr data-row="${index}">
          <td><input class="row-check" type="checkbox" data-select="${index}" aria-label="选择第 ${index + 1} 项"${selectable ? "" : " disabled"} /></td>
          <td>${thumbnail}</td>
          <td class="url-cell"${preview}>${htmlEscape(resource.source || value)}</td>
          <td><span class="pill">${htmlEscape(localizeResourceType(resource))}</span></td>
          <td class="detail-cell">${resourceDetail(resource, isVideo, hasSupportedQuality, index)}</td>
          <td>
            ${isVideo
              ? `<button type="button" data-download="${index}"${hasSupportedQuality ? "" : " disabled"}>${hasSupportedQuality ? "下载 MP4" : "不可下载"}</button>`
              : `<button type="button" data-copy="${index}">${resource.content_type === "text" ? "复制文字" : "复制链接"}</button>`}
          </td>
        </tr>
        `;
      }).join("");

      results.innerHTML = `
        <div class="result-toolbar">
          <div class="selection-summary" id="selectionSummary">共 ${resources.length} 项，可选择 ${resources.filter(resource => (resource.content_type === "video" || !resource.content_type) && resource.qualities.some(q => q.supported)).length} 个视频</div>
          <div class="toolbar-actions">
            <button class="secondary" type="button" id="selectAllButton">全选视频</button>
            <button type="button" id="downloadSelectedButton" disabled>下载选中</button>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th><input class="select-all" id="selectAllCheckbox" type="checkbox" aria-label="全选可下载视频" /></th>
              <th>封面</th>
              <th>名称 / 来源</th>
              <th>类型</th>
              <th>清晰度</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;

      const selectAllCheckbox = results.querySelector("#selectAllCheckbox");
      const selectAllButton = results.querySelector("#selectAllButton");
      const downloadSelectedButton = results.querySelector("#downloadSelectedButton");

      function updateSelectionUi() {
        results.querySelectorAll("[data-select]").forEach(input => {
          const index = Number(input.dataset.select);
          input.checked = selectedResources.has(index);
          results.querySelector(`[data-row="${index}"]`).classList.toggle("is-selected", input.checked);
        });
        const selectable = Array.from(results.querySelectorAll("[data-select]:not(:disabled)"));
        selectAllCheckbox.checked = selectable.length > 0 && selectedResources.size === selectable.length;
        selectAllCheckbox.indeterminate = selectedResources.size > 0 && selectedResources.size < selectable.length;
        selectAllButton.textContent = selectAllCheckbox.checked ? "取消全选" : "全选视频";
        downloadSelectedButton.disabled = selectedResources.size === 0;
        document.querySelector("#selectionSummary").textContent = `已选择 ${selectedResources.size} 个视频，共 ${resources.length} 项`;
      }

      function toggleAll() {
        const selectable = Array.from(results.querySelectorAll("[data-select]:not(:disabled)"));
        const shouldSelect = selectedResources.size !== selectable.length;
        selectedResources.clear();
        if (shouldSelect) selectable.forEach(input => selectedResources.add(Number(input.dataset.select)));
        updateSelectionUi();
      }

      results.querySelectorAll("[data-select]").forEach(input => input.addEventListener("change", () => {
        const index = Number(input.dataset.select);
        input.checked ? selectedResources.add(index) : selectedResources.delete(index);
        updateSelectionUi();
      }));
      selectAllCheckbox.addEventListener("change", toggleAll);
      selectAllButton.addEventListener("click", toggleAll);
      downloadSelectedButton.addEventListener("click", async () => {
        downloadSelectedButton.disabled = true;
        const indexes = Array.from(selectedResources);
        for (const index of indexes) {
          const select = results.querySelector(`[data-quality="${index}"]`);
          if (select) await startDownload(resources[index].qualities[Number(select.value)]);
        }
        selectedResources.clear();
        updateSelectionUi();
      });

      results.querySelectorAll("[data-download]").forEach(button => {
        button.addEventListener("click", () => {
          const index = Number(button.dataset.download);
          const select = results.querySelector(`[data-quality="${index}"]`);
          if (!select) return;
          const quality = resources[index].qualities[Number(select.value)];
          startDownload(quality);
        });
      });

      results.querySelectorAll("[data-copy]").forEach(button => {
        button.addEventListener("click", async () => {
          const resource = resources[Number(button.dataset.copy)];
          const originalText = button.textContent;
          button.disabled = true;
          try {
            await copyValue(resourceValue(resource));
            button.textContent = "已复制";
          } catch (error) {
            button.textContent = "复制失败";
          } finally {
            window.setTimeout(() => {
              button.textContent = originalText;
              button.disabled = false;
            }, 900);
          }
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
            ${canRemove(task) ? `<button class="secondary" type="button" data-remove="${task.id}">删除记录</button>` : ""}
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

      queue.querySelectorAll("[data-remove]").forEach(button => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await postJson(`/api/tasks/${button.dataset.remove}/remove`, {});
            tasks.delete(button.dataset.remove);
            renderQueue();
          } catch (error) {
            button.disabled = false;
            alert(error.message);
          }
        });
      });
    }

    function canCancel(task) {
      return task.status === "queued" || task.status === "running" || task.status === "converting";
    }

    function canRemove(task) {
      return task.status === "done" || task.status === "failed" || task.status === "cancelled";
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

    importCookiesButton.addEventListener("click", () => {
      cookiesImportInput.click();
    });

    selectFolderButton.addEventListener("click", async () => {
      selectFolderButton.disabled = true;
      try {
        const data = await postJson("/api/select-folder", { initial_dir: outputDirInput.value.trim() });
        if (data.path) outputDirInput.value = data.path;
      } catch (error) {
        alert(error.message);
      } finally {
        selectFolderButton.disabled = false;
      }
    });

    cookiesImportInput.addEventListener("change", async () => {
      const file = cookiesImportInput.files && cookiesImportInput.files[0];
      if (!file) return;
      importCookiesButton.disabled = true;
      try {
        const content = await file.text();
        const data = await postJson("/api/cookies/import", {
          filename: file.name,
          content,
          url: urlInput.value.trim()
        });
        cookiesFileInput.value = data.path;
        scanStatus.textContent = "Cookies 已导入";
      } catch (error) {
        alert(error.message);
      } finally {
        importCookiesButton.disabled = false;
        cookiesImportInput.value = "";
      }
    });

    async function startDownload(quality) {
      const task = await postJson("/api/download", {
        ...quality,
        output_dir: outputDirInput.value.trim(),
        cookies_file: cookiesFileInput.value.trim()
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
        const contentTypes = selectedContentTypes();
        if (!contentTypes.length) throw new Error("请至少选择一种抓取内容。");
        const data = await postJson("/api/scan", {
          url: urlInput.value.trim(),
          content_types: contentTypes,
          cookies_file: cookiesFileInput.value.trim()
        });
        renderResults(data.resources);
        scanStatus.textContent = `发现 ${data.resources.length} 个`;
        if (data.warning) scanStatus.textContent += ` · ${data.warning}`;
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


AGENT_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>paqu 每日视频 Agent</title>
<style>
:root{--bg:#f4f0e8;--panel:#fffdf8;--ink:#17211d;--muted:#65716c;--line:#d7d8ce;--accent:#146c5b;--danger:#a33c32;--warn:#a56717}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#edf3ef,#f7f0e3);color:var(--ink);font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif}.shell{width:min(1320px,calc(100% - 28px));margin:24px auto 50px}header,.toolbar,.panel{background:var(--panel);border:1px solid var(--line);box-shadow:0 12px 34px #22352b12}header{padding:24px 28px;border-radius:20px;display:flex;justify-content:space-between;gap:18px;align-items:center}h1{font-size:27px;margin:0 0 4px}.sub{color:var(--muted)}a{color:var(--accent)}button,select,input{font:inherit}button{border:0;border-radius:10px;padding:10px 15px;background:var(--accent);color:white;font-weight:700;cursor:pointer}button.alt{background:#e7ece8;color:var(--ink)}button.danger{background:var(--danger)}button.mini{margin-top:7px;padding:6px 9px;border-radius:7px;font-size:12px}button:disabled{opacity:.5;cursor:not-allowed}.toolbar{margin-top:14px;border-radius:16px;padding:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.toolbar select,.toolbar input{border:1px solid var(--line);border-radius:9px;padding:9px;background:white}.status{margin-left:auto;color:var(--muted)}.panel{margin-top:14px;border-radius:18px;overflow:hidden}.summary{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center}.table-wrap{overflow:auto;max-height:68vh}table{width:100%;border-collapse:collapse;min-width:1100px}th,td{padding:11px;border-bottom:1px solid #e7e7df;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#f0f3ef;z-index:2;font-size:12px;text-transform:uppercase;color:var(--muted)}.thumb{width:128px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;background:#ddd}.title{font-weight:750;max-width:360px}.reason{max-width:320px;color:var(--muted)}.pill{display:inline-block;border-radius:99px;padding:3px 8px;font-size:12px;font-weight:700;background:#e7ece8}.progress-track{width:150px;height:8px;margin-top:6px;border-radius:99px;background:#dfe5e1;overflow:hidden}.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),#2aa884);transition:width .25s ease}.progress-text{margin-top:3px;color:var(--muted);font-size:12px;white-space:nowrap}.high{color:var(--accent)}.review{color:var(--warn)}.low{color:var(--danger)}.empty{padding:50px;text-align:center;color:var(--muted)}@media(max-width:800px){header{align-items:flex-start;flex-direction:column}.status{margin-left:0}}
</style></head><body><main class="shell">
<header><div><h1>每日视频发现 Agent</h1><div class="sub">近 30 天 · 每次最多新增 100 个高相关候选 · 人工勾选下载</div><div class="sub" id="modelStatus">模型配置读取中…</div></div><div><a href="/rag">视觉 RAG 知识库</a> · <a href="/">单链接爬取工具</a></div></header>
<section class="toolbar">
  <button id="run">立即运行</button><button class="alt" id="refresh">刷新</button>
  <select id="topic"><option value="">全部主题</option></select>
  <input id="date" type="date"><input id="agentOutputDir" type="text" value="downloads" placeholder="视频保存目录"><button class="alt" id="selectAgentFolder">选择文件夹</button><span class="status" id="runStatus">待命</span>
</section>
<section class="panel"><div class="summary"><strong id="count">0 个高相关候选</strong><button class="danger" id="deleteCandidates">删除选中</button><button id="download">下载选中</button><span class="sub" id="actionStatus">系统只展示高相关结果；勾选后可删除或下载</span></div><div class="table-wrap" id="table"><div class="empty">正在读取候选…</div></div></section>
</main><script>
const $=s=>document.querySelector(s), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});const d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d}
function selected(){return [...document.querySelectorAll('.pick:checked')].map(x=>Number(x.value))}
function reviewLabel(v){return ({pending:'待处理',kept:'已保留',excluded:'已排除',deleted:'已删除'})[v]||v}
function downloadLabel(v){return ({not_requested:'未下载',queued:'排队中',downloading:'下载中',done:'已完成',failed:'失败'})[v]||v}
function formatBytes(v){const n=Number(v||0);if(!n)return '0 B';const u=['B','KB','MB','GB'];const i=Math.min(Math.floor(Math.log(n)/Math.log(1024)),u.length-1);return `${(n/1024**i).toFixed(i?1:0)} ${u[i]}`}
function downloadProgress(c){if(c.download_status==='done')return '<div class="progress-track"><div class="progress-fill" style="width:100%"></div></div><div class="progress-text">100% · 下载完成</div>';if(!['queued','downloading'].includes(c.download_status))return '';const done=Number(c.downloaded_bytes||0),total=Number(c.total_bytes||0),pct=total?Math.min(100,Math.round(done*100/total)):0;return `<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div><div class="progress-text">${total?`${pct}% · ${formatBytes(done)} / ${formatBytes(total)}`:'正在获取文件大小…'}</div>`}
function qs(){const p=new URLSearchParams();for(const id of ['topic','date'])if($('#'+id).value)p.set(id,$('#'+id).value);return p.toString()}
async function load(reselect=[]){try{const d=await api('/api/agent/candidates?'+qs());$('#count').textContent=`${d.candidates.length} 个高相关候选`;$('#table').innerHTML=d.candidates.length?`<table><thead><tr><th><input type="checkbox" id="all"></th><th>预览</th><th>主题 / 相关度</th><th>视频</th><th>判定理由</th><th>下载</th></tr></thead><tbody>${d.candidates.map(c=>`<tr><td><input class="pick" type="checkbox" value="${c.id}"${reselect.includes(c.id)?' checked':''}></td><td>${c.thumbnail?`<img class="thumb" src="${esc(c.thumbnail)}" loading="lazy">`:''}</td><td><div>${esc(c.topic)}</div><span class="pill high">高相关 · ${c.score}</span>${c.rag_similarity!=null?`<div class="progress-text">视觉相似度 ${Math.round(Number(c.rag_similarity)*100)}%</div>`:''}</td><td><div class="title"><a href="${esc(c.canonical_url)}" target="_blank" rel="noreferrer">${esc(c.title)}</a></div><div class="sub">${esc(c.channel)} · ${esc(c.published_at||'日期未知')}<br>搜索：${esc(c.search_query)}</div></td><td class="reason">${esc(c.reason)}</td><td>${esc(downloadLabel(c.download_status))}${downloadProgress(c)}${c.saved_to?`<br>${esc(c.saved_to)}${c.download_status==='done'?`<br><button class="mini" data-open-agent-folder="${c.id}">打开文件夹</button>`:''}`:''}${c.error?`<br><span class="low">${esc(c.error)}</span>`:''}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">本次没有新的高相关候选。</div>';const all=$('#all');if(all)all.onchange=()=>document.querySelectorAll('.pick').forEach(x=>x.checked=all.checked);document.querySelectorAll('[data-open-agent-folder]').forEach(button=>button.onclick=async()=>{try{const d=await api('/api/agent/open-folder',{method:'POST',body:JSON.stringify({id:Number(button.dataset.openAgentFolder)})});$('#actionStatus').textContent=`已打开：${d.folder}`}catch(e){alert(e.message)}})}catch(e){$('#table').innerHTML=`<div class="empty low">${esc(e.message)}</div>`}}
async function pollDownloads(ids){for(let attempt=0;attempt<3600;attempt++){await new Promise(r=>setTimeout(r,1000));const d=await api('/api/agent/candidates');await load(ids);const tracked=d.candidates.filter(c=>ids.includes(c.id));if(!tracked.some(c=>['queued','downloading'].includes(c.download_status)))break}}
async function review(status){const ids=selected();if(!ids.length)return alert('请先勾选候选。');try{const d=await api('/api/agent/review',{method:'POST',body:JSON.stringify({ids,status})});const action={kept:'保留',excluded:'排除',deleted:'删除'}[status]||'更新';$('#actionStatus').textContent=`已${action} ${d.changed} 个${status==='deleted'?'；候选已从列表隐藏，视频文件未删除':'；勾选继续保留'}`;await load(status==='deleted'?[]:ids)}catch(e){$('#actionStatus').textContent=`操作失败：${e.message}`;alert(e.message)}}
async function state(){const d=await api('/api/agent/status');$('#runStatus').textContent=d.status==='running'?'Agent 正在搜索…':d.status==='failed'?`失败：${d.error}`:d.status==='done'?`完成：新增 ${d.new_count||0} / 候选 ${d.candidate_count||0}`:'待命';$('#run').disabled=d.status==='running';if(d.status==='running')setTimeout(state,3000)}
$('#run').onclick=async()=>{try{await api('/api/agent/run',{method:'POST',body:JSON.stringify({force:true})});state()}catch(e){alert(e.message)}};$('#refresh').onclick=()=>load();$('#deleteCandidates').onclick=()=>review('deleted');$('#selectAgentFolder').onclick=async()=>{try{const d=await api('/api/select-folder',{method:'POST',body:JSON.stringify({initial_dir:$('#agentOutputDir').value.trim()})});if(d.path)$('#agentOutputDir').value=d.path}catch(e){alert(e.message)}};$('#download').onclick=async()=>{const ids=selected();if(!ids.length)return alert('请先勾选要下载的候选。');try{const d=await api('/api/agent/download',{method:'POST',body:JSON.stringify({ids,output_dir:$('#agentOutputDir').value.trim()})});$('#actionStatus').textContent=d.queued.length?`已加入下载队列 ${d.queued.length} 个，保存到 ${d.output_dir}（按主题分文件夹）`:'没有加入下载：所选视频可能已完成或正在下载';await load(ids);if(d.queued.length)pollDownloads(d.queued)}catch(e){$('#actionStatus').textContent=`下载失败：${e.message}`;alert(e.message)}};for(const id of ['topic','date'])$('#'+id).onchange=()=>load();
(async()=>{const c=await api('/api/agent/config');$('#topic').innerHTML+=[...c.topics].map(t=>`<option value="${esc(t.key)}">${esc(t.name)}</option>`).join('');$('#modelStatus').textContent=`DeepSeek ${c.models.deepseek.configured?'已配置':'未配置'} (${c.models.deepseek.model}) · Qwen视觉 ${c.models.qwen.configured?'已配置':'未配置'} (${c.models.qwen.model})`;await Promise.all([load(),state()])})();
</script></body></html>"""


RAG_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>视觉 RAG 知识库</title><style>
:root{--bg:#eef3f0;--panel:#fffdf8;--ink:#17211d;--muted:#65716c;--line:#d7d8ce;--accent:#146c5b;--danger:#a33c32}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#e9f2ed,#f7f0e3);color:var(--ink);font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif}.shell{width:min(1100px,calc(100% - 28px));margin:24px auto 50px}header,.panel{background:var(--panel);border:1px solid var(--line);box-shadow:0 12px 34px #22352b12;border-radius:18px}header{padding:24px 28px;display:flex;justify-content:space-between;align-items:center;gap:20px}h1{margin:0 0 4px;font-size:27px}.sub{color:var(--muted)}a{color:var(--accent)}.panel{margin-top:14px;padding:18px}.upload{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}button,input{font:inherit}input[type=file],input[type=text]{border:1px solid var(--line);border-radius:9px;padding:10px;background:white}button{border:0;border-radius:9px;padding:10px 14px;background:var(--accent);color:white;font-weight:700;cursor:pointer}button.danger{background:var(--danger)}button.alt{background:#e6ece8;color:var(--ink)}button:disabled{opacity:.5}.status{margin-top:10px;color:var(--muted)}table{width:100%;border-collapse:collapse;margin-top:8px}th,td{padding:12px;border-bottom:1px solid #e7e7df;text-align:left;vertical-align:middle}th{color:var(--muted);background:#f0f3ef}.name-edit{display:flex;gap:8px}.name-edit input{min-width:220px;flex:1}.actions{display:flex;gap:8px}.empty{padding:38px;text-align:center;color:var(--muted)}@media(max-width:700px){header{align-items:flex-start;flex-direction:column}.upload{grid-template-columns:1fr}.actions,.name-edit{flex-wrap:wrap}}
</style></head><body><main class="shell">
<header><div><h1>视觉 RAG 知识库</h1><div class="sub">管理用于画面相似检索的样本视频和关键帧索引</div></div><div><a href="/agent">返回视频发现 Agent</a></div></header>
<section class="panel"><div class="upload"><input id="ragFile" type="file" accept="video/*" multiple><button id="upload">批量上传并建立索引</button></div><div class="status" id="status">可一次选择多个视频；单个视频最大 250 MB，每个样本最多抽取 12 张关键帧。</div></section>
<section class="panel"><strong id="count">0 个样本</strong><div id="list"><div class="empty">正在读取知识库…</div></div></section>
</main><script>
const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});const d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d}
async function load(){try{const d=await api('/api/agent/rag');$('#count').textContent=`${d.samples.length} 个样本`;$('#list').innerHTML=d.samples.length?`<table><thead><tr><th>名称</th><th>关键帧</th><th>创建时间</th><th>本地文件</th><th>操作</th></tr></thead><tbody>${d.samples.map(s=>`<tr><td><div class="name-edit"><input type="text" value="${esc(s.name)}" data-name="${s.id}"><button class="alt" data-save="${s.id}">保存名称</button></div></td><td>${s.frame_count}</td><td>${esc(s.created_at)}</td><td class="sub">${esc(s.file_path)}</td><td><div class="actions"><button class="danger" data-delete="${s.id}">删除</button></div></td></tr>`).join('')}</tbody></table>`:'<div class="empty">知识库为空，请上传你想要的视频样本。</div>';document.querySelectorAll('[data-save]').forEach(b=>b.onclick=()=>rename(Number(b.dataset.save)));document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>removeSample(Number(b.dataset.delete)))}catch(e){$('#list').innerHTML=`<div class="empty">${esc(e.message)}</div>`}}
async function rename(id){const input=document.querySelector(`[data-name="${id}"]`);try{await api('/api/agent/rag/update',{method:'POST',body:JSON.stringify({id,name:input.value})});$('#status').textContent='名称已保存。';await load()}catch(e){alert(e.message)}}
async function removeSample(id){if(!confirm('确定删除这个 RAG 样本及其单个上传文件吗？'))return;try{await api('/api/agent/rag/delete',{method:'POST',body:JSON.stringify({id})});$('#status').textContent='样本及索引已删除。';await load()}catch(e){alert(e.message)}}
async function fileData(file){return await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',',2)[1]);r.onerror=reject;r.readAsDataURL(file)})}
$('#upload').onclick=async()=>{const files=[...$('#ragFile').files];if(!files.length)return alert('请先选择一个或多个视频文件。');$('#upload').disabled=true;let success=0;const failures=[];try{for(let i=0;i<files.length;i++){const file=files[i];$('#status').textContent=`正在处理 ${i+1}/${files.length}：${file.name}`;if(file.size>250*1024*1024){failures.push(`${file.name}：超过 250 MB`);continue}try{await api('/api/agent/rag/upload',{method:'POST',body:JSON.stringify({name:file.name,data:await fileData(file)})});success++}catch(e){failures.push(`${file.name}：${e.message}`)}}$('#ragFile').value='';$('#status').textContent=`批量处理完成：成功 ${success}/${files.length}${failures.length?`；失败 ${failures.length}：${failures.join('；')}`:''}`;await load()}finally{$('#upload').disabled=false}};load();
</script></body></html>"""


def agent_store() -> AgentStore:
    global AGENT_STORE
    if AGENT_STORE is None:
        AGENT_STORE = AgentStore()
    return AGENT_STORE


def start_agent_run(force: bool = False, limit: int = 100, window_days: int = 30) -> bool:
    with AGENT_RUN_LOCK:
        if AGENT_RUN_STATE.get("status") == "running":
            return False
        AGENT_RUN_STATE.clear()
        AGENT_RUN_STATE.update({"status": "running", "started_at": time.time()})

    def worker() -> None:
        try:
            cookies = default_cookies_file_for_url("https://www.youtube.com/")
            result = run_discovery(agent_store(), limit=limit, window_days=window_days, force=force, cookies_file=cookies)
            with AGENT_RUN_LOCK:
                AGENT_RUN_STATE.clear()
                AGENT_RUN_STATE.update(result)
        except Exception as exc:
            with AGENT_RUN_LOCK:
                AGENT_RUN_STATE.clear()
                AGENT_RUN_STATE.update({"status": "failed", "error": str(exc)})

    threading.Thread(target=worker, name="paqu-agent-discovery", daemon=True).start()
    return True


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def scan_page_resources(
    url: str,
    content_types: list[str] | None = None,
    cookies_file: Path | None = None,
) -> tuple[HTTPStatus, dict[str, Any]]:
    selected = normalize_content_types(content_types)
    resources = []
    warning = None

    if "video" in selected:
        if weixin_finder_id(url):
            weixin_resources = discover_weixin_finder_resources(url, REQUEST_TIMEOUT)
            resources.extend(weixin_resources)
            if weixin_resources:
                selected = selected - {"video"}
        elif url.lower().split("?", 1)[0].endswith((".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".m3u8", ".mpd", "/stodownload")):
            direct_resources = discover_video_resources(url, DEFAULT_USER_AGENT, REQUEST_TIMEOUT)
            resources.extend(direct_resources)
            selected = selected - {"video"}
        else:
            ytdlp_result = discover_ytdlp(url, REQUEST_TIMEOUT, cookies_file)
            ytdlp_resources = ytdlp_result.resources
            resources.extend(ytdlp_resources)
            if ytdlp_resources:
                selected = selected - {"video"}
            elif ytdlp_result.terminal or (selected == {"video"} and ytdlp_result.error):
                error = friendly_ytdlp_error(ytdlp_result.error, cookies_file)
                return HTTPStatus.UNPROCESSABLE_ENTITY, {"error": f"yt-dlp 未能解析该视频：{error}"}
            else:
                weixin_resources = discover_weixin_finder_resources(url, REQUEST_TIMEOUT)
                resources.extend(weixin_resources)
                if weixin_resources:
                    selected = selected - {"video"}

    if selected:
        if not can_fetch(url, DEFAULT_USER_AGENT):
            message = "robots.txt 不允许使用 Scrapling 抓取该页面。"
            if resources:
                return HTTPStatus.OK, {"resources": [asdict(resource) for resource in resources], "warning": message}
            return HTTPStatus.FORBIDDEN, {"error": f"yt-dlp 未能识别可下载视频，且 {message}"}
        try:
            resources.extend(discover_scrapling_content(url, DEFAULT_USER_AGENT, REQUEST_TIMEOUT, selected))
        except PermissionError as exc:
            if resources:
                return HTTPStatus.OK, {"resources": [asdict(resource) for resource in resources], "warning": str(exc)}
            return HTTPStatus.FORBIDDEN, {"error": str(exc)}

    return HTTPStatus.OK, {"resources": [asdict(resource) for resource in resources], "warning": warning}


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def validate_cookies_content(content: str, site: str) -> None:
    if not content.strip():
        raise ValueError("Cookies 文件是空的。")
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Cookies 文件过大，请确认导出的是 cookies.txt。")
    lowered = content.lower()
    expected_domains = {
        "youtube": ("youtube.com", "youtu.be"),
        "douyin": ("douyin.com",),
        "facebook": ("facebook.com",),
        "tiktok": ("tiktok.com",),
    }[site]
    if not any(domain in lowered for domain in expected_domains):
        site_name = {"youtube": "YouTube", "douyin": "抖音", "facebook": "Facebook", "tiktok": "TikTok"}[site]
        raise ValueError(f"这不像 {site_name} cookies.txt，请确认是在该网站页面导出的 Netscape 格式 cookies。")
    if "\t" not in content:
        raise ValueError("Cookies 文件格式不正确；请导出 Netscape cookies.txt 格式。")


def save_site_cookies(content: str, url: str | None) -> Path:
    if is_youtube_url(url):
        site, filename = "youtube", DEFAULT_YOUTUBE_COOKIES_FILE
    elif is_douyin_url(url):
        site, filename = "douyin", DEFAULT_DOUYIN_COOKIES_FILE
    elif is_facebook_url(url):
        site, filename = "facebook", DEFAULT_FACEBOOK_COOKIES_FILE
    elif is_tiktok_url(url):
        site, filename = "tiktok", DEFAULT_TIKTOK_COOKIES_FILE
    else:
        raise ValueError("请先输入 YouTube、抖音、TikTok 或 Facebook 视频链接，再导入对应网站的 cookies.txt。")
    validate_cookies_content(content, site)
    target = Path.cwd() / filename
    target.write_text(content, encoding="utf-8")
    return target


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


def is_youtube_url(url: str | None) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    host = parsed.netloc.lower().removeprefix("www.")
    return host in {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}


def is_douyin_url(url: str | None) -> bool:
    host = urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")
    return host == "douyin.com" or host.endswith(".douyin.com")


def is_facebook_url(url: str | None) -> bool:
    host = urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")
    return host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.watch"


def is_tiktok_url(url: str | None) -> bool:
    host = urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def cookie_file_matches_url(cookies_file: Path, url: str | None) -> bool:
    name = cookies_file.name.lower()
    if "youtube" in name or "youtu.be" in name:
        return is_youtube_url(url)
    if "douyin" in name:
        return is_douyin_url(url)
    if "facebook" in name or name.startswith("fb."):
        return is_facebook_url(url)
    if "tiktok" in name:
        return is_tiktok_url(url)
    return True


def default_cookies_file_for_url(url: str | None) -> Path | None:
    if is_youtube_url(url):
        filenames = YOUTUBE_COOKIE_FILENAMES
    elif is_douyin_url(url):
        filenames = DOUYIN_COOKIE_FILENAMES
    elif is_facebook_url(url):
        filenames = FACEBOOK_COOKIE_FILENAMES
    elif is_tiktok_url(url):
        filenames = TIKTOK_COOKIE_FILENAMES
    else:
        return None
    for filename in filenames:
        candidate = Path.cwd() / filename
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def friendly_ytdlp_error(message: str | None, cookies_file: Path | None = None) -> str:
    text = message or "yt-dlp 未能识别该视频。"
    lowered = text.lower()
    if "curl: (77)" in lowered or "error setting certificate verify locations" in lowered:
        return "TLS 证书配置异常。请关闭当前服务窗口并重新运行 start_server.bat；程序将使用系统默认的 yt-dlp HTTPS 配置。"
    if "tiktok" in lowered and any(marker in lowered for marker in ("log in for access", "login", "cookies-from-browser", "--cookies")):
        if cookies_file:
            return f"TikTok 要求登录后访问；已尝试使用 {cookies_file}，但仍被拒绝。请在浏览器登录 TikTok，重新导出最新 Netscape 格式 cookies.txt 后再扫描。"
        return "TikTok 要求登录后访问。请在浏览器登录 TikTok，导出 Netscape 格式 cookies.txt，再点击“导入当前网站 cookies”后重新扫描。"
    if "sign in to confirm" in lowered and "not a bot" in lowered:
        if cookies_file:
            return f"YouTube 要求登录态验证；已尝试使用 {cookies_file}，但仍被拒绝。请重新导出最新 YouTube cookies.txt 后再扫描。"
        return "YouTube 要求登录态验证。请导出 YouTube cookies.txt，或把文件命名为 www.youtube.com_cookies.txt 放在项目目录后重新扫描。"
    if "fresh cookies" in lowered:
        if cookies_file:
            return f"抖音要求新的 cookies；{cookies_file} 已失效。请在浏览器打开抖音并完成正常验证后，重新导出 Netscape 格式 cookies.txt，再通过页面按钮导入。"
        return "抖音要求 cookies。请在浏览器打开抖音后导出 Netscape 格式 cookies.txt，再通过页面按钮导入。"
    if "facebook" in lowered or any(marker in lowered for marker in ("cannot parse data", "requested content is not available", "login required")):
        if cookies_file:
            return f"Facebook 未返回可下载的视频；已尝试使用 {cookies_file}。请确认该视频可在浏览器播放，并重新导出最新 Facebook Netscape 格式 cookies.txt。原始错误：{text}"
        return f"Facebook 可能要求登录态。请在浏览器登录 Facebook，导出 Netscape 格式 cookies.txt，再点击“导入当前网站 cookies”后重新扫描。原始错误：{text}"
    return text


def resolve_cookies_file(value: str | None, url: str | None = None) -> Path | None:
    raw_value = (value or "").strip()
    if not raw_value:
        return default_cookies_file_for_url(url)
    cookies_file = Path(raw_value)
    if not cookies_file.is_absolute():
        cookies_file = Path.cwd() / cookies_file
    if not cookies_file.exists():
        raise ValueError("Cookies 文件不存在。")
    if not cookies_file.is_file():
        raise ValueError("Cookies 路径必须是文件，不能是文件夹。")
    if not cookie_file_matches_url(cookies_file, url):
        raise ValueError("Cookies 文件与当前链接的网站不匹配；请清空该输入，或换成当前网站对应的 cookies.txt。")
    return cookies_file


def create_task(quality: VideoQuality, output_dir: Path | None = None, cookies_file: Path | None = None) -> DownloadTask:
    now = time.time()
    resolved_output_dir = output_dir or resolve_output_dir(None)
    task = DownloadTask(
        id=uuid4().hex,
        url=quality.url,
        quality_label=quality.label,
        kind=quality.kind,
        format_id=quality.format_id,
        output_dir=str(resolved_output_dir),
        cookies_file=str(cookies_file) if cookies_file else None,
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


def remove_task(task_id: str) -> tuple[bool, str | None]:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return False, "任务不存在。"
        if task.status not in {"done", "failed", "cancelled"}:
            return False, "只能删除已完成、失败或已取消的任务记录。"
        del TASKS[task_id]
        return True, None


def folder_for_task(task: DownloadTask) -> Path:
    if task.saved_to:
        saved_path = Path(task.saved_to)
        return saved_path.parent if saved_path.suffix else saved_path
    return Path(task.output_dir)


def folder_for_candidate(candidate: dict[str, Any]) -> Path:
    saved_to = str(candidate.get("saved_to") or "").strip()
    if candidate.get("download_status") != "done" or not saved_to:
        raise ValueError("该视频尚未下载完成。")
    saved_path = Path(saved_to)
    if not saved_path.is_file():
        raise FileNotFoundError("下载文件已被移动或不存在。")
    return saved_path.parent


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
        cookies_file = Path(task.cookies_file) if task.cookies_file else None

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
                cookies_file,
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

        if parsed.path == "/agent":
            body = AGENT_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/rag":
            body = RAG_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/agent/config":
            json_response(self, HTTPStatus.OK, {"topics": topic_payload(), "limit": 100, "window_days": 30, "models": model_status()})
            return

        if parsed.path == "/api/agent/status":
            with AGENT_RUN_LOCK:
                state = dict(AGENT_RUN_STATE)
            json_response(self, HTTPStatus.OK, state)
            return

        if parsed.path == "/api/agent/candidates":
            query = urllib.parse.parse_qs(parsed.query)
            filters = {key: values[0] for key, values in query.items() if values}
            filters["relevance"] = "高相关"
            json_response(self, HTTPStatus.OK, {"candidates": agent_store().list_candidates(filters)})
            return

        if parsed.path == "/api/agent/runs":
            json_response(self, HTTPStatus.OK, {"runs": agent_store().list_runs()})
            return

        if parsed.path == "/api/agent/rag":
            json_response(self, HTTPStatus.OK, {"samples": agent_store().list_rag_samples(), "threshold": 0.70})
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
            if self.path == "/api/cookies/import":
                self.handle_import_cookies()
                return
            if self.path == "/api/agent/run":
                self.handle_agent_run()
                return
            if self.path == "/api/agent/review":
                self.handle_agent_review()
                return
            if self.path == "/api/agent/download":
                self.handle_agent_download()
                return
            if self.path == "/api/agent/open-folder":
                self.handle_agent_open_folder()
                return
            if self.path == "/api/agent/rag/upload":
                self.handle_agent_rag_upload()
                return
            if self.path == "/api/agent/rag/update":
                self.handle_agent_rag_update()
                return
            if self.path == "/api/agent/rag/delete":
                self.handle_agent_rag_delete()
                return
            if self.path == "/api/select-folder":
                self.handle_select_folder()
                return
            if self.path.startswith("/api/tasks/") and self.path.endswith("/cancel"):
                self.handle_cancel()
                return
            if self.path.startswith("/api/tasks/") and self.path.endswith("/remove"):
                self.handle_remove()
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

        content_types = payload.get("content_types")
        if content_types is not None and not isinstance(content_types, list):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "抓取内容参数无效。"})
            return

        try:
            cookies_file = resolve_cookies_file(payload.get("cookies_file"), url)
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        status, response = scan_page_resources(url, content_types, cookies_file)
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
            cookies_file = resolve_cookies_file(payload.get("cookies_file"), quality.url)
        except OSError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"无法创建保存目录：{exc}"})
            return
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        task = create_task(quality, output_dir, cookies_file)
        json_response(self, HTTPStatus.OK, serialize_task(task))

    def handle_import_cookies(self) -> None:
        payload = read_json(self)
        content = str(payload.get("content", ""))
        try:
            target = save_site_cookies(content, str(payload.get("url", "")))
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(self, HTTPStatus.OK, {"path": str(target)})

    def handle_agent_run(self) -> None:
        payload = read_json(self)
        force = bool(payload.get("force", False))
        limit = max(1, min(int(payload.get("limit", 100)), 100))
        window_days = max(1, min(int(payload.get("window_days", 30)), 365))
        if not start_agent_run(force, limit, window_days):
            json_response(self, HTTPStatus.CONFLICT, {"error": "Agent 已在运行中。"})
            return
        json_response(self, HTTPStatus.ACCEPTED, {"status": "running"})

    def handle_agent_review(self) -> None:
        payload = read_json(self)
        ids = payload.get("ids") or []
        if not isinstance(ids, list):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "候选 ID 列表无效。"})
            return
        try:
            changed = agent_store().update_review(ids, str(payload.get("status", "")))
            agent_store().export_csv()
        except (TypeError, ValueError) as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(self, HTTPStatus.OK, {"changed": changed})

    def handle_agent_download(self) -> None:
        payload = read_json(self)
        ids = payload.get("ids") or []
        if not isinstance(ids, list):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "候选 ID 列表无效。"})
            return
        try:
            cookies = default_cookies_file_for_url("https://www.youtube.com/")
            output_dir = resolve_output_dir(str(payload.get("output_dir", "")))
            agent_store().keep_for_download(ids)
            queued = download_candidates(agent_store(), ids, cookies, root=output_dir)
        except (OSError, TypeError, ValueError) as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(self, HTTPStatus.ACCEPTED, {"queued": queued, "output_dir": str(output_dir)})

    def handle_select_folder(self) -> None:
        payload = read_json(self)
        initial = str(payload.get("initial_dir", "")).strip()
        initial_dir = initial if initial and Path(initial).is_dir() else str(Path.cwd())
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(initialdir=initial_dir, title="选择视频保存目录", mustexist=True)
        finally:
            root.destroy()
        json_response(self, HTTPStatus.OK, {"path": selected})

    def handle_agent_open_folder(self) -> None:
        payload = read_json(self)
        try:
            candidate_id = int(payload.get("id"))
        except (TypeError, ValueError):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "候选 ID 无效。"})
            return
        candidate = agent_store().get_candidate(candidate_id)
        if not candidate:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "候选不存在。"})
            return
        try:
            folder = folder_for_candidate(candidate)
            open_folder(folder)
        except FileNotFoundError as exc:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        except (OSError, ValueError) as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        json_response(self, HTTPStatus.OK, {"ok": True, "folder": str(folder)})

    def handle_agent_rag_upload(self) -> None:
        payload = read_json(self)
        original_name = str(payload.get("name") or "").strip()
        encoded = str(payload.get("data") or "")
        if not original_name or not encoded:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "请选择要上传的视频文件。"})
            return
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "样本必须是常见视频格式。"})
            return
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "上传的视频数据无效。"})
            return
        if len(content) > 250 * 1024 * 1024:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "单个样本视频不能超过 250 MB。"})
            return
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original_name).stem).strip("._") or "sample"
        RAG_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = RAG_UPLOAD_DIR / f"{uuid4().hex}_{safe_stem[:60]}{suffix}"
        target.write_bytes(content)
        try:
            hashes = extract_video_hashes(target)
            sample_id = agent_store().add_rag_sample(original_name, target, hashes)
        except Exception as exc:
            target.unlink(missing_ok=True)
            json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": f"关键帧索引失败：{exc}"})
            return
        json_response(self, HTTPStatus.OK, {"id": sample_id, "name": original_name, "frame_count": len(hashes)})

    def handle_agent_rag_update(self) -> None:
        payload = read_json(self)
        try:
            sample_id = int(payload.get("id"))
            updated = agent_store().rename_rag_sample(sample_id, str(payload.get("name") or ""))
        except (TypeError, ValueError) as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if not updated:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "RAG 样本不存在。"})
            return
        json_response(self, HTTPStatus.OK, {"ok": True})

    def handle_agent_rag_delete(self) -> None:
        payload = read_json(self)
        try:
            sample_id = int(payload.get("id"))
        except (TypeError, ValueError):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "RAG 样本 ID 无效。"})
            return
        sample = agent_store().get_rag_sample(sample_id)
        if not sample:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "RAG 样本不存在。"})
            return
        target = Path(str(sample["file_path"])).resolve()
        allowed_root = RAG_UPLOAD_DIR.resolve()
        if not target.is_relative_to(allowed_root):
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "样本文件路径不在 RAG 上传目录中。"})
            return
        try:
            if target.exists():
                target.unlink()
            deleted = agent_store().delete_rag_sample(sample_id)
        except OSError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": f"无法删除样本文件：{exc}"})
            return
        json_response(self, HTTPStatus.OK, {"ok": deleted})

    def handle_cancel(self) -> None:
        task_id = self.path.split("/")[-2]
        task = cancel_task(task_id)
        if not task:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "任务不存在。"})
            return
        json_response(self, HTTPStatus.OK, serialize_task(task))

    def handle_remove(self) -> None:
        task_id = self.path.split("/")[-2]
        removed, error = remove_task(task_id)
        if not removed:
            status = HTTPStatus.NOT_FOUND if error == "任务不存在。" else HTTPStatus.BAD_REQUEST
            json_response(self, status, {"error": error})
            return
        json_response(self, HTTPStatus.OK, {"ok": True})

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
    if os.environ.get("PAQU_OPEN_BROWSER", "").strip() == "1":
        start_url = f"http://{HOST}:{PORT}/agent"
        threading.Timer(0.4, webbrowser.open, args=(start_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
