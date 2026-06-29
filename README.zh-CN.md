# Jellyfin External Player

> [English](README.md) · [中文](README.zh-CN.md)

把 Jellyfin 网页的播放交给 Windows 上的 PotPlayer，关闭播放器时进度自动同步回 Jellyfin。

## 🚀 一键安装

**直接点 — Tampermonkey 自动识别安装 userscript；EXE 是单文件下载：**

| 组件 | 链接 |
|---|---|
| Userscript（Tampermonkey） | **[点击安装 jellyfin-external-player.user.js](https://raw.githubusercontent.com/shawnlu96/jellyfin-external-player/main/src/userscript.user.js)** |
| Helper EXE（Windows） | 见 [Releases](../../releases) 页 — 下载 `JellyfinExternalPlayer.exe` |

---

- ✅ 原生 PotPlayer 播放质量（HDR、环绕声、硬解）
- ✅ "继续观看"正常工作 — 关 PotPlayer 时进度回报给 Jellyfin
- ✅ 续播位置精准 — 从 Jellyfin 上次记录的位置打开
- ✅ Jellyfin 通用识别 — 局域网 / 自定义域名 / 反向代理 / 子路径全支持
- ✅ 主题无关浮动按钮 — JellyFlix、自定义 CSS、任何 Jellyfin 版本都能用
- ✅ 零配置 — 自动探测 PotPlayer 安装路径
- ✅ 单文件 EXE — 不需要装 Python

## 工作原理

```
Jellyfin web (Chrome + Tampermonkey)
    │  POST 127.0.0.1:54321/play
    ▼
JellyfinExternalPlayer.exe (系统托盘)
    │  启 PotPlayer 带 /seek=续播位置
    ▼
PotPlayer (你看片)
    │  你关闭 PotPlayer
    ▼
helper 读最后位置 + POST {jellyfin}/Sessions/Playing/Stopped
    ▼
Jellyfin "继续观看"更新 ✓
```

helper 用的是你**当前已登录 Jellyfin 的 session token**（从浏览器里的 `window.ApiClient.accessToken()` 读取）——不存密码，不要额外凭证。Token 只在你局域网内流动：Jellyfin → 浏览器 → 127.0.0.1 helper → Jellyfin。

## 安装

### 1. 本地 helper

1. 从 [Releases](../../releases) 下载 `JellyfinExternalPlayer.exe`。
2. 扔到任意目录（推荐 `%LOCALAPPDATA%\JellyfinExternalPlayer\`）。
3. 双击运行 — 系统托盘出现图标。
4. 右键托盘图标 → **Start with Windows** 设置开机自启。

### 2. Userscript

1. 在 Chrome / Edge / Firefox 装 [Tampermonkey](https://www.tampermonkey.net/)。
2. 点击 **[这个 raw 链接](https://raw.githubusercontent.com/shawnlu96/jellyfin-external-player/main/src/userscript.user.js)** — Tampermonkey 自动弹安装窗口。
3. 打开 Jellyfin 网页 → 任意电影/电视剧详情页 → 右下角出现 **▶ PotPlayer** 浮动按钮。

## 系统要求

- Windows 10 / 11
- [PotPlayer](https://potplayer.daum.net/)（通过注册表 + 常见路径自动探测）
- Chrome / Edge / Firefox + [Tampermonkey](https://www.tampermonkey.net/)
- Jellyfin 10.8+（兼容 JellyFlix 主题和任何自定义 CSS）

## 从源码构建

需要 Python 3.12。

```bash
git clone https://github.com/shawnlu96/jellyfin-external-player
cd jellyfin-external-player
pip install -r requirements.txt
python src/helper.py        # 源码直跑
build.bat                   # 输出 dist\JellyfinExternalPlayer.exe
```

或者推一个 `v*` tag —— GitHub Actions 自动 build EXE 并发 release。

## 进度同步机制

PotPlayer 不提供实时进度 API，所以本 helper 用**关闭即同步**策略：

1. PotPlayer 退出时，读注册表 `HKCU\Software\DAUM\PotPlayerMini64\RecentFileList\Position{N}` 拿启动的文件最后位置。
2. POST `{jellyfin}/Sessions/Playing/Stopped` 带 `PositionTicks`，Jellyfin 记录停止位置。

**边缘情况**：如果你没正常关 PotPlayer（断电、崩溃、任务管理器强杀），进度回退到 "启动时间 + 经过秒数" 估算 — 一般场景够用。

要保证多端切换（PC ↔ 手机/iPad）时进度精准，**切换前先关闭 PotPlayer**。

## 故障排查

- **Jellyfin 页面没出现 PotPlayer 按钮** → 浏览器 DevTools 控制台看有没有 `[jellyfin-external-player] Jellyfin detected, userscript active`。没有的话 Tampermonkey 没跑 或者 userscript 没装。
- **弹 "Local helper not running"** → 启动 `JellyfinExternalPlayer.exe`，看托盘图标。
- **PotPlayer 没启动** → 托盘菜单显示探测到的 PotPlayer 路径。如果是 "NOT FOUND"，先装 PotPlayer。
- **进度没同步** → 打开 `%LOCALAPPDATA%\JellyfinExternalPlayer\helper.log`，找 `Stopped reported: 204`。
- **"Jellyfin SDK not loaded"** → 页面加载后等一下再点；某些主题加载慢。

## 许可证

MIT
