const { app, BrowserWindow, ipcMain } = require('electron')
const path = require('path')
const fs = require('fs')
const https = require('https')

const UPDATE_SERVER = 'https://fastapi-four-ai.onrender.com'
const CACHE_DIR = path.join(app.getPath('userData'), 'app-cache')
const CHANNEL_FILE = path.join(app.getPath('userData'), 'channel.json')

// ========== 工具函数 ==========

function getStoredChannel() {
  try {
    if (fs.existsSync(CHANNEL_FILE)) {
      const data = JSON.parse(fs.readFileSync(CHANNEL_FILE, 'utf-8'))
      if (data.channel === 'latest' || data.channel === 'stable') return data.channel
    }
  } catch (_) {}
  return 'stable'
}

function httpGet(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { timeout: 15000 }, (res) => {
      if (res.statusCode !== 200) { reject(new Error(`HTTP ${res.statusCode}`)); return }
      let data = ''
      res.on('data', chunk => data += chunk)
      res.on('end', () => resolve(data))
    }).on('error', reject)
  })
}

async function downloadFile(url, destPath) {
  const content = await httpGet(url)
  const dir = path.dirname(destPath)
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(destPath, content, 'utf-8')
}

// ========== 启动闪屏 ==========

function createSplash() {
  const splash = new BrowserWindow({
    width: 460,
    height: 300,
    frame: false,
    transparent: false,
    alwaysOnTop: true,
    resizable: false,
    center: true,
    backgroundColor: '#0f172a',
    webPreferences: { nodeIntegration: false, contextIsolation: true }
  })

  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; background:#0f172a; color:#e2e8f0; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; user-select:none; }
  .logo { font-size:32px; font-weight:bold; background:linear-gradient(135deg,#3b82f6,#8b5cf6); -webkit-background-clip:text; -webkit-text-fill-color:transparent; margin-bottom:6px; }
  .subtitle { font-size:13px; color:#64748b; margin-bottom:28px; }
  .status-row { display:flex; align-items:center; gap:10px; margin-bottom:8px; font-size:13px; }
  .dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
  .dot.blue { background:#3b82f6; animation:pulse 1.2s infinite; }
  .dot.green { background:#22c55e; }
  .dot.red { background:#ef4444; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .progress-bar { width:320px; height:4px; background:#1e293b; border-radius:2px; margin:16px 0 6px; overflow:hidden; }
  .progress-fill { height:100%; width:0%; background:linear-gradient(90deg,#3b82f6,#8b5cf6); border-radius:2px; transition:width 0.3s; }
  .detail { font-size:11px; color:#475569; margin-top:4px; }
  .channel-tag { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; margin-left:6px; }
  .channel-tag.stable { background:#166534; color:#86efac; }
  .channel-tag.latest { background:#1e3a5f; color:#93c5fd; }
</style></head>
<body>
  <div class="logo">AI 智能审题工作台</div>
  <div class="subtitle">多模态 · 交叉验证 · 一键交付</div>
  <div class="status-row"><span class="dot blue" id="dot"></span><span id="status">正在启动...</span></div>
  <div class="progress-bar"><div class="progress-fill" id="bar"></div></div>
  <div class="detail"><span id="detail"></span><span class="channel-tag stable" id="chan" style="display:none"></span></div>
</body></html>`

  splash.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
  return splash
}

function splashUpdate(splash, status, detail, pct, channel) {
  try {
    splash.webContents.executeJavaScript(`
      (function(){
        var s=document.getElementById('status'); if(s) s.innerText=${JSON.stringify(status)};
        var d=document.getElementById('detail'); if(d) d.innerText=${JSON.stringify(detail||'')};
        var b=document.getElementById('bar'); if(b) b.style.width='${pct||0}%';
        var dot=document.getElementById('dot');
        if(dot){ dot.className='dot ${pct>=100 ? 'green' : 'blue'}'; }
        var c=document.getElementById('chan');
        if(c && ${JSON.stringify(channel||'')}){
          c.style.display='inline-block'; c.innerText=${JSON.stringify(channel)};
          c.className='channel-tag ' + ${JSON.stringify(channel)};
        }
      })()
    `).catch(() => {})
  } catch (_) {}
}

// ========== 版本检查与下载 ==========

async function checkForUpdates(splash) {
  const channel = getStoredChannel()
  splashUpdate(splash, '正在连接更新服务器...', UPDATE_SERVER, 5, channel)

  // 1. 获取版本清单
  let manifest
  try {
    const versionJson = await httpGet(`${UPDATE_SERVER}/static/version.json`)
    manifest = JSON.parse(versionJson)
  } catch (err) {
    splashUpdate(splash, '无法连接服务器', '将使用本地内置版本启动', 0)
    return { cacheDir: null, version: null, channel, manifest: null }
  }

  const targetVersion = manifest[channel]
  if (!targetVersion || !manifest.versions[targetVersion]) {
    splashUpdate(splash, '未找到可用版本', '通道: ' + channel, 0)
    return { cacheDir: null, version: null, channel, manifest }
  }

  const versionInfo = manifest.versions[targetVersion]
  splashUpdate(splash, '发现版本 ' + targetVersion, versionInfo.note || '', 25, channel)

  // 2. 下载文件
  const versionDir = path.join(CACHE_DIR, targetVersion)
  const files = versionInfo.files.filter(f => f !== 'main.js')

  for (let i = 0; i < files.length; i++) {
    const file = files[i]
    const pct = 25 + Math.round((i + 1) / files.length * 70)
    splashUpdate(splash,
      '正在下载 ' + targetVersion,
      file + '  (' + (i + 1) + '/' + files.length + ')',
      pct, channel
    )

    try {
      await downloadFile(
        `${UPDATE_SERVER}/static/versions/${targetVersion}/${file}`,
        path.join(versionDir, file)
      )
    } catch (err) {
      splashUpdate(splash, '下载失败', file + ': ' + err.message, 0)
      return { cacheDir: null, version: null, channel, manifest }
    }
  }

  splashUpdate(splash, '更新完成', targetVersion + ' · ' + channel + '通道 · 即将启动', 100, channel)
  return { cacheDir: versionDir, version: targetVersion, channel, manifest }
}

// ========== 主窗口 ==========

function createMainWindow(updateInfo) {
  const mainWindow = new BrowserWindow({
    width: 1300,
    height: 850,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false
    }
  })

  mainWindow.setMenu(null)

  const indexPath = updateInfo.cacheDir
    ? path.join(updateInfo.cacheDir, 'index.html')
    : path.join(__dirname, 'index.html')

  mainWindow.loadFile(indexPath)

  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
  })

  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      mainWindow.webContents.toggleDevTools()
      event.preventDefault()
    }
  })

  // 注入版本信息
  mainWindow.webContents.on('did-finish-load', () => {
    mainWindow.webContents.executeJavaScript(`
      window.__APP_VERSION__ = ${JSON.stringify(updateInfo.version)};
      window.__APP_CHANNEL__ = ${JSON.stringify(updateInfo.channel)};
      window.__APP_MANIFEST__ = ${JSON.stringify(updateInfo.manifest)};
    `)
  })

  return mainWindow
}

// ========== IPC ==========

ipcMain.handle('get-channel', () => getStoredChannel())

ipcMain.handle('set-channel', (event, channel) => {
  if (channel !== 'stable' && channel !== 'latest') return false
  fs.writeFileSync(CHANNEL_FILE, JSON.stringify({ channel }), 'utf-8')
  return true
})

ipcMain.handle('get-app-info', () => ({
  channel: getStoredChannel(),
  userData: app.getPath('userData')
}))

// ========== 启动流程 ==========

app.whenReady().then(async () => {
  // 1. 显示闪屏
  const splash = createSplash()

  // 2. 检查更新（带进度）
  const updateInfo = await checkForUpdates(splash)

  // 3. 短暂停留让用户看清状态
  await new Promise(r => setTimeout(r, 600))

  // 4. 关闭闪屏，打开主窗口
  splash.close()
  const mainWindow = createMainWindow(updateInfo)

  app.on('activate', function () {
    if (BrowserWindow.getAllWindows().length === 0) {
      // Re-check on activate
      // For simplicity, just create with last known cache
      const channel = getStoredChannel()
      // ...
    }
  })
})

app.on('window-all-closed', function () {
  if (process.platform !== 'darwin') app.quit()
})
