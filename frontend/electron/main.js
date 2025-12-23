const { app, BrowserWindow } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const isDev = process.env.NODE_ENV === 'development';

let mainWindow;
let backendProcess;

// Ensure single instance
const gotTheLock = app.requestSingleInstanceLock();

if (!gotTheLock) {
    console.log('Another instance is already running. Quitting...');
    app.quit();
} else {
    app.on('second-instance', (event, commandLine, workingDirectory) => {
        // Someone tried to run a second instance, focus our window instead
        if (mainWindow) {
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.focus();
        }
    });
}

function startBackend() {
    console.log('Starting backend...');

    // In production, the backend is an executable in the resources folder
    // In development, we use the local python server
    if (!isDev) {
        const backendPath = path.join(process.resourcesPath, 'SmartTraderBackend', 'SmartTraderBackend.exe');
        console.log('Backend path:', backendPath);

        backendProcess = spawn(backendPath, [], {
            cwd: path.dirname(backendPath),
            detached: false
        });

        backendProcess.stdout.on('data', (data) => {
            console.log(`Backend: ${data}`);
        });

        backendProcess.stderr.on('data', (data) => {
            console.error(`Backend Error: ${data}`);
        });

        backendProcess.on('close', (code) => {
            console.log(`Backend process exited with code ${code}`);
        });
    } else {
        console.log('Development mode - expecting backend to run separately');
    }
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1400,
        height: 900,
        backgroundColor: '#050505',
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            preload: path.join(__dirname, 'preload.js')
        },
        titleBarStyle: 'hiddenInset'
    });

    if (isDev) {
        mainWindow.loadURL('http://localhost:3000');
        mainWindow.webContents.openDevTools();
    } else {
        // Load the static export
        mainWindow.loadFile(path.join(__dirname, '../out/index.html'));
        // Uncomment for debugging production build:
        // mainWindow.webContents.openDevTools();
    }

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

app.on('ready', () => {
    startBackend();
    createWindow();
});

app.on('window-all-closed', () => {
    // Always quit and cleanup backend on all platforms
    if (backendProcess) {
        console.log('Killing backend process...');
        backendProcess.kill();
    }
    app.quit();
});

app.on('activate', () => {
    if (mainWindow === null) {
        createWindow();
    }
});

// Ensure backend cleanup on app quit
app.on('will-quit', () => {
    if (backendProcess) {
        console.log('App quitting - killing backend...');
        backendProcess.kill('SIGTERM');
    }
});
