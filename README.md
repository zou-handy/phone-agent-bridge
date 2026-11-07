# Phone Agent Bridge (`phone-agent-bridge`)

> **跨端 AI Agent 移动端受控浏览器桥接、系统级小窗保活、USB 零登录态反控电脑与云沙箱远程 FUSE 缓存加速套件**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Android%20PRoot%20%7C%20ColorOS%2016%20%7C%20Windows-10b981.svg)](#)
[![Protocol](https://img.shields.io/badge/Protocol-Chrome%20CDP%20%7C%20ADB%20%7C%20go--fuse-2563eb.svg)](#)

本项目沉淀了在移动端 Linux 容器（Android PRoot Ubuntu + Antigravity CLI / Claude Code）与云端 Agent 沙箱中构建全天候跨端自动化工作流的核心工具链：

1. **移动端满血浏览器 CDP 接管与系统级迷你小窗保活（`launch-mini` + `connect_mobile_edge.sh`）**：打通手机端 Microsoft Edge / Chrome 的完整 `9222` CDP 接口（600+ 原语，100% 复用真实登录状态与浏览器扩展），并通过 ColorOS 16 系统窗口参数将浏览器固定在右上角 `261x464` 贴边迷你小窗（`state=2`），既不遮挡主屏幕操作，又防止后台 `cached=true` 冻结套接字。
2. **手机 USB 一线零登录态反控机房/办公电脑（`pc-run`）**：基于 `adb reverse` 本地回环隧道，手机插上电脑 USB 线即可由手机容器内 Agent 直接调度电脑端 PowerShell / Python / MySQL / Word 自动化任务，支持拔线 2 秒自动重连，电脑端零账号登录、拔线零痕迹。
3. **云端 Agent 沙箱远程文件挂载（`go-fuse`）目录遍历加速（`es_sqlite_cache.py`）**：针对云端沙箱通过 WebSocket 挂载本地目录（`go-fuse`）时 POSIX 细粒度串行系统调用（`LOOKUP`/`GETATTR`）放大网络 RTT 的瓶颈，调用本机 Everything (`es.exe`) 毫秒级导出 NTFS MFT 目录树快照至沙箱内存 SQLite（FTS5），实现零网络 RTT 的秒级路径检索。

---

## 📂 仓库结构

```text
phone-agent-bridge/
├── README.md
├── LICENSE
└── scripts/
    ├── launch-mini             # ColorOS 16 右上角贴边迷你小窗 (state=2) 零触摸唤醒器
    ├── connect_mobile_edge.sh  # 手机端 Edge/Chrome 抽象套接字探测与 9222 CDP 端口映射
    ├── pc-run                  # 基于 adb reverse 的手机反控电脑命令执行器（支持断线自愈）
    └── es_sqlite_cache.py      # Everything (es.exe) NTFS 目录树导出至 SQLite FTS5 缓存工具
```

---

## 1. 移动端满血 Edge/Chrome CDP + 右上角迷你小窗保活

### 痛点分析
- 移动端内置 WebView 包装器通常仅支持极少数简单的 JS 注入接口，缺乏真实事件派发、多标签页管理、扩展生态与文件流上传能力。
- Android 版 **Microsoft Edge (`com.microsoft.emmx`)** 与 **Chrome (`com.android.chrome`)** 原生暴露 `@chrome_devtools_remote` 抽象域套接字，支持完整 Chromium CDP 1.3 协议，且 Edge 支持同时运行 Tampermonkey 等浏览器扩展。
- 但一旦浏览器切入后台，移动操作系统会在数秒内将其标记为 `cached=true` 并冻结网络与 CDP 套接字；而普通自由浮窗（`--windowingMode 5`）在后台已有全屏任务时会被忽略，或以居中大浮窗（`873x1552`）遮挡屏幕。

### 解决方案（`scripts/launch-mini` & `scripts/connect_mobile_edge.sh`）
通过分析 ColorOS 16 窗口管理参数，在启动 Activity 时传入 `--windowingMode 100` 配合 `androidx.activity.LaunchScenario=1` 与 `androidx.activity.LaunchScale=0.24166666`，紧接着执行 `cmd activity task resize <TASK_ID> 759 120 1839 2040`，即可零触摸将浏览器直接钉在屏幕右上角 `261x464` 迷你小窗（`state=2`）：
- 容器内 Agent 通过 `127.0.0.1:9222` 发送 CDP 指令直接作用于浏览器内部 DOM/V8 视口，不依赖物理屏幕坐标点击；
- 浏览器处于前台可见的迷你小窗态，100% 免除后台冻结，同时不干扰用户正常使用手机主屏幕。

```bash
# 一键将手机端 Edge 唤醒至右上角迷你小窗并映射 127.0.0.1:9222 CDP 端口
./scripts/connect_mobile_edge.sh
curl -s http://127.0.0.1:9222/json/version
```

---

## 2. 手机 USB 一线零登录态反控电脑（`scripts/pc-run`）

在公共机房或临时办公电脑上，无需在电脑端登录个人账号或安装大体积 IDE：
1. 电脑端仅需开启轻量回环监听或由手机通过 ADB 建立 `adb reverse` 反向隧道；
2. 手机端容器内的 Coding Agent 直接通过 `pc-run "<command>"` 在电脑端执行编译、数据库查询或文档生成，并将产物拉回手机存储。

---

## 3. 云端沙箱远程 FUSE 遍历瓶颈分析与 Everything 快照缓存（`scripts/es_sqlite_cache.py`）

### 根因剖析
云端 Agent 沙箱通过 `go-fuse` over WebSocket 将用户本地磁盘挂载到云端容器内时，POSIX 文件系统接口是细粒度串行的：在挂载目录执行一次递归搜索会触发成千上万次独立的 `LOOKUP` -> `OPENDIR` -> `READDIR` -> `GETATTR` 请求。即使单次网络 RTT 仅为 15ms，串行放大数千次后也会导致长达数十秒的卡顿。

### 零 RTT 缓存方案
利用 Windows 本机 Everything CLI (`es.exe`) 直接读取 NTFS MFT 主文件表的特性（毫秒级输出全量路径、大小与修改时间），将其批量导入 SQLite FTS5 全文索引表。沙箱内 Agent 检索文件时直接查询内存 SQLite（`< 2ms`），仅对命中的目标文件发起按需流式读取。

```bash
# 导出指定工作区目录树快照至 SQLite FTS5 数据库
python scripts/es_sqlite_cache.py --root "C:\Users" --db workspace_index.db --query "manifest.json"
```

---

## 📄 License

[MIT License](LICENSE)
