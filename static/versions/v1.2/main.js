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

// ========== 版本检查与热更新 ==========

async function checkForUpdates() {
  try {
    const versionJson = await httpGet(`${UPDATE_SERVER}/static/version.json`)
    const manifest = JSON.parse(versionJson)
    const channel = getStoredChannel()
    const targetVersion = manifest[channel]

    if (!targetVersion || !manifest.versions[targetVersion]) {
      console.log(`[Updater] No valid version for channel: ${channel}`)
      return { cacheDir: null, version: null, channel, manifest }
    }

    const versionDir = path.join(CACHE_DIR, targetVersion)
    const files = manifest.versions[targetVersion].files.filter(f => f !== 'main.js')

    for (const file of files) {
      const destPath = path.join(versionDir, file)
      await downloadFile(`${UPDATE_SERVER}/static/versions/${targetVersion}/${file}`, destPath)
    }

    console.log(`[Updater] Channel=${channel}, Version=${targetVersion}`)
    return { cacheDir: versionDir, version: targetVersion, channel, manifest }
  } catch (err) {
    console.log(`[Updater] Failed: ${err.message}, using bundled files`)
    return { cacheDir: null, version: null, channel: getStoredChannel(), manifest: null }
  }
}

// ========== 窗口创建 ==========

async function createWindow() {
  const updateInfo = await checkForUpdates()

  const mainWindow = new BrowserWindow({
    width: 1300,
    height: 850,
    minWidth: 1024,
    minHeight: 700,
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

  // F12 打开开发者工具
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      mainWindow.webContents.toggleDevTools()
      event.preventDefault()
    }
  })

  // 页面加载完成后，注入版本信息
  mainWindow.webContents.on('did-finish-load', () => {
    mainWindow.webContents.executeJavaScript(`
      window.__APP_VERSION__ = ${JSON.stringify(updateInfo.version)};
      window.__APP_CHANNEL__ = ${JSON.stringify(updateInfo.channel)};
      window.__APP_MANIFEST__ = ${JSON.stringify(updateInfo.manifest)};
    `)
  })
}

// ========== IPC：通道切换（渲染进程调用） ==========

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

// ========== 启动 ==========

app.whenReady().then(() => {
  createWindow()

  app.on('activate', function () {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', function () {
  if (process.platform !== 'darwin') app.quit()
})
