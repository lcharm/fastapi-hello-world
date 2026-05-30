const { app, BrowserWindow, ipcMain } = require('electron')
const path = require('path')
const fs = require('fs')
const https = require('https')

// 服务器地址：环境变量 UPDATE_SERVER 优先 → config.json 中的 serverUrl → 默认 Render
// 迁移到阿里云时只需设置环境变量或修改 config.json，无需改代码
let UPDATE_SERVER = 'https://fastapi-four-ai.onrender.com'
try {
  const configPath = path.join(__dirname, 'config.json')
  if (fs.existsSync(configPath)) {
    const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'))
    if (config.serverUrl) UPDATE_SERVER = config.serverUrl
  }
} catch (_) { /**/ }
if (process.env.UPDATE_SERVER) UPDATE_SERVER = process.env.UPDATE_SERVER
const CACHE_DIR = path.join(app.getPath('userData'), 'app-cache')

// 获取缓存中最新的版本号（作为更新失败时的回退显示）
function getLatestCachedVersion() {
  try {
    if (!fs.existsSync(CACHE_DIR)) return null
    const dirs = fs.readdirSync(CACHE_DIR).filter(f =>
      f.startsWith('v') && fs.statSync(path.join(CACHE_DIR, f)).isDirectory()
    )
    if (dirs.length === 0) return null
    dirs.sort().reverse()
    return dirs[0]
  } catch (_) { return null }
}

// ========== 工具函数 ==========

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
    height: 280,
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
  .detail { font-size:11px; color:#475569; margin-top:4px; max-width:380px; text-align:center; }
</style></head>
<body>
  <div class="logo">AI 智能审题工作台</div>
  <div class="subtitle">多模态 · 交叉验证 · 一键交付</div>
  <div class="status-row"><span class="dot blue" id="dot"></span><span id="status">正在启动...</span></div>
  <div class="progress-bar"><div class="progress-fill" id="bar"></div></div>
  <div class="detail"><span id="detail"></span></div>
</body></html>`

  splash.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
  return splash
}

function splashUpdate(splash, status, detail, pct) {
  try {
    splash.webContents.executeJavaScript(`
      (function(){
        var s=document.getElementById('status'); if(s) s.innerText=${JSON.stringify(status)};
        var d=document.getElementById('detail'); if(d) d.innerText=${JSON.stringify(detail||'')};
        var b=document.getElementById('bar'); if(b) b.style.width='${pct||0}%';
        var dot=document.getElementById('dot');
        if(dot){ dot.className='dot ${pct>=100 ? 'green' : pct===0 ? 'red' : 'blue'}'; }
      })()
    `).catch(() => {})
  } catch (_) {}
}

// ========== 版本检查与下载 ==========

async function checkForUpdates(splash) {
  splashUpdate(splash, '正在连接更新服务器...', UPDATE_SERVER, 5)

  // 1. 获取版本清单（带重试唤醒，解决 Render 免费实例休眠问题）
  //    迁移到阿里云后可简化：删除重试循环，只保留单次 httpGet
  let manifest
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt > 0) {
      splashUpdate(splash, '等待服务器唤醒...', '第 ' + attempt + ' 次重试（共 3 次）', 8)
      await new Promise(r => setTimeout(r, 8000))
    }
    try {
      const versionJson = await httpGet(`${UPDATE_SERVER}/static/version.json`)
      manifest = JSON.parse(versionJson)
      break
    } catch (err) {
      if (attempt === 3) {
        const fallbackVer1 = getLatestCachedVersion()
        const fallbackDir1 = fallbackVer1 ? path.join(CACHE_DIR, fallbackVer1) : null
        splashUpdate(splash, '无法连接服务器', fallbackVer1 ? '将使用缓存版本 ' + fallbackVer1 : '将使用内置版本启动', 0)
        return { cacheDir: fallbackDir1, version: fallbackVer1, manifest: null, updateFailed: true }
      }
    }
  }

  const targetVersion = manifest.current
  if (!targetVersion || !manifest.versions || !manifest.versions[targetVersion]) {
    const fallbackVer2 = getLatestCachedVersion()
    const fallbackDir2 = fallbackVer2 ? path.join(CACHE_DIR, fallbackVer2) : null
    splashUpdate(splash, '版本清单无效', fallbackVer2 ? '将使用缓存版本 ' + fallbackVer2 : '将使用内置版本启动', 0)
    return { cacheDir: fallbackDir2, version: fallbackVer2, manifest, updateFailed: true }
  }

  const versionInfo = manifest.versions[targetVersion]
  splashUpdate(splash, '发现版本 ' + targetVersion, versionInfo.note || '', 25)

  // 2. 下载文件
  const versionDir = path.join(CACHE_DIR, targetVersion)
  const files = versionInfo.files.filter(f => f !== 'main.js')

  for (let i = 0; i < files.length; i++) {
    const file = files[i]
    const pct = 25 + Math.round((i + 1) / files.length * 70)
    splashUpdate(splash, '正在下载 ' + targetVersion, file + '  (' + (i + 1) + '/' + files.length + ')', pct)

    try {
      await downloadFile(
        `${UPDATE_SERVER}/static/versions/${targetVersion}/${file}`,
        path.join(versionDir, file)
      )
    } catch (err) {
      const fallbackVer3 = getLatestCachedVersion()
      const fallbackDir3 = fallbackVer3 ? path.join(CACHE_DIR, fallbackVer3) : null
      splashUpdate(splash, '下载失败', file + ': ' + err.message + (fallbackVer3 ? '，回退到缓存版本 ' + fallbackVer3 : ''), 0)
      return { cacheDir: fallbackDir3, version: fallbackVer3, manifest, updateFailed: true }
    }
  }

  splashUpdate(splash, '更新完成', targetVersion + ' · 即将启动', 100)
  return { cacheDir: versionDir, version: targetVersion, manifest, updateFailed: false }
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

  mainWindow.once('ready-to-show', () => mainWindow.show())

  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      mainWindow.webContents.toggleDevTools()
      event.preventDefault()
    }
  })

  // 注入版本信息与服务器地址（渲染进程轮询读取，兼容 IPC 未就绪的情况）
  mainWindow.webContents.on('did-finish-load', () => {
    mainWindow.webContents.executeJavaScript(`
      window.__APP_VERSION__ = ${JSON.stringify(updateInfo.version)};
      window.__APP_MANIFEST__ = ${JSON.stringify(updateInfo.manifest)};
      window.__APP_UPDATE_FAILED__ = ${JSON.stringify(updateInfo.updateFailed)};
      window.__API_BASE__ = ${JSON.stringify(UPDATE_SERVER)};
    `)
  })

  return mainWindow
}

// ========== IPC ==========

// 模块级存储，渲染进程通过 IPC 拉取
let currentUpdateInfo = { version: null, manifest: null, updateFailed: true }

ipcMain.handle('get-version-info', () => currentUpdateInfo)

ipcMain.handle('retry-update', async () => {
  // 前台触发的重试下载：直接拉到最新版覆盖缓存
  try {
    const versionJson = await httpGet(`${UPDATE_SERVER}/static/version.json`)
    const manifest = JSON.parse(versionJson)
    const targetVersion = manifest.current
    if (!targetVersion) return { ok: false, error: '无可用版本' }

    const versionDir = path.join(CACHE_DIR, targetVersion)
    const files = manifest.versions[targetVersion].files.filter(f => f !== 'main.js')
    for (const file of files) {
      await downloadFile(`${UPDATE_SERVER}/static/versions/${targetVersion}/${file}`, path.join(versionDir, file))
    }
    return { ok: true, version: targetVersion }
  } catch (err) {
    return { ok: false, error: err.message }
  }
})

// ========== 启动流程 ==========

app.whenReady().then(async () => {
  const splash = createSplash()
  const updateInfo = await checkForUpdates(splash)
  // 存入模块变量，供渲染进程通过 IPC 拉取
  currentUpdateInfo = {
    version: updateInfo.version,
    manifest: updateInfo.manifest,
    updateFailed: updateInfo.updateFailed
  }
  await new Promise(r => setTimeout(r, 600))
  splash.close()
  createMainWindow(updateInfo)

  app.on('activate', function () {
    if (BrowserWindow.getAllWindows().length === 0) {
      // macOS dock 点击时重新创建窗口（此处简化处理）
    }
  })
})

app.on('window-all-closed', function () {
  if (process.platform !== 'darwin') app.quit()
})
