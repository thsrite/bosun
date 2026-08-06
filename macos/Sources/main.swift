// Bosun 状态栏常驻程序（LSUIElement，无 Dock 图标）。
//
// 职责边界：本程序**不持有**后端进程，只做状态显示与控制。
// 后端由 launchd 持有（com.thsrite.bosun.backend），因此本程序崩溃或退出
// 都不会连累后端下面正在跑的 Claude 任务。
//
// 资源约束（硬性）：稳态下不派生任何子进程，仅 15s 一次 localhost 健康探测，
// 定时器带容差以便系统合并唤醒，休眠/锁屏期间完全挂起。
// launchctl 只在用户主动点击菜单项时才调用。

import AppKit
import Darwin
import Foundation

// MARK: - 常量与路径

let kBackendLabel = "com.thsrite.bosun.backend"
let kMenubarLabel = "com.thsrite.bosun.menubar"

let fm = FileManager.default
let homeDir = fm.homeDirectoryForCurrentUser
let launchAgentsDir = homeDir.appendingPathComponent("Library/LaunchAgents")
let logsDir = homeDir.appendingPathComponent("Library/Logs")
let backendPlistURL = launchAgentsDir.appendingPathComponent("\(kBackendLabel).plist")
let menubarPlistURL = launchAgentsDir.appendingPathComponent("\(kMenubarLabel).plist")
let outLogURL = logsDir.appendingPathComponent("bosun.out.log")
let errLogURL = logsDir.appendingPathComponent("bosun.err.log")
let guiDomain = "gui/\(getuid())"

let kAutoOpenKey = "autoOpenWorkbench"
let workbenchURL = URL(string: "http://127.0.0.1:\(BosunConfig.port)")!
let healthURL = URL(string: "http://127.0.0.1:\(BosunConfig.port)/api/health")!

// MARK: - 子进程（仅用户主动操作时调用）

@discardableResult
func runProcess(_ executable: String, _ arguments: [String]) -> (code: Int32, output: String) {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: executable)
    task.arguments = arguments
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = pipe
    do {
        try task.run()
    } catch {
        return (-1, "\(error)")
    }
    // 先读到 EOF 再 wait，避免管道写满导致子进程阻塞。
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    task.waitUntilExit()
    return (task.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

@discardableResult
func launchctl(_ arguments: [String]) -> (code: Int32, output: String) {
    runProcess("/bin/launchctl", arguments)
}

// MARK: - LaunchAgent plist

/// 后端服务的 LaunchAgent 定义。
/// ThrottleInterval 是防崩溃风暴的闸门：即便后端反复启动失败，
/// launchd 也最多每 10 秒重试一次，不会打满 CPU。
func backendPlistDict(runAtLoad: Bool) -> [String: Any] {
    [
        "Label": kBackendLabel,
        "ProgramArguments": [BosunConfig.python, "run.py"],
        "WorkingDirectory": BosunConfig.backendDir,
        "EnvironmentVariables": [
            "BOSUN_HOST": "0.0.0.0",
            "BOSUN_PORT": String(BosunConfig.port),
            // launchd 不读 .zshrc，claude / node / git 的路径必须显式给全。
            "PATH": BosunConfig.path,
            "HOME": homeDir.path,
            "LANG": "zh_CN.UTF-8",
        ],
        "RunAtLoad": runAtLoad,
        "KeepAlive": ["SuccessfulExit": false],
        "ThrottleInterval": 10,
        "StandardOutPath": outLogURL.path,
        "StandardErrorPath": errLogURL.path,
        "ProcessType": "Interactive",
    ]
}

/// 状态栏程序自身的 LaunchAgent，仅用于登录自启。
func menubarPlistDict() -> [String: Any] {
    [
        "Label": kMenubarLabel,
        "ProgramArguments": [Bundle.main.executablePath ?? BosunConfig.executableFallback],
        "RunAtLoad": true,
        // 图标进程不需要保活：用户主动退出就该保持退出。
        "KeepAlive": false,
        "ProcessType": "Interactive",
    ]
}

func writePlist(_ dict: [String: Any], to url: URL) throws {
    try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    let data = try PropertyListSerialization.data(fromPropertyList: dict, format: .xml, options: 0)
    try data.write(to: url, options: .atomic)
}

func readPlist(_ url: URL) -> [String: Any]? {
    guard let data = try? Data(contentsOf: url) else { return nil }
    return (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any]
}

// MARK: - 应用主体

final class BosunController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var timer: Timer?
    private var isRunning = false
    private var menuIsOpen = false
    private var didAutoOpen = false

    /// 进程指标。CPU 用两次采样之间的累计 CPU 时间差算占用率，
    /// 比 ps 的「进程生命周期平均值」更能反映当前状态。
    private struct Usage {
        var rss: UInt64 = 0
        var percent: Double = 0
        var lastCPUNanos: UInt64 = 0
        var lastAt: TimeInterval = 0
    }
    private var selfUsage = Usage()
    private var backendUsage = Usage()
    private var cachedBackendPID: pid_t?
    private var taskCounts: [String: Int] = [:]
    private var backendVersion: String?
    private var latestVersion: String?
    private var updateAvailable = false
    private var releaseURL: String?

    /// 独立的短超时会话：不落 cookie、不写缓存，避免后台产生额外 IO。
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 1.5
        config.timeoutIntervalForResource = 2.0
        config.httpMaximumConnectionsPerHost = 1
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        return URLSession(configuration: config)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: [kAutoOpenKey: true])  // 默认启动即打开工作台
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.toolTip = "Bosun"
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        render(running: false)

        ensureBackendPlistExists()
        startBackendIfNeeded()

        startTimer()
        observeSleepWake()
        probe()

        if UserDefaults.standard.bool(forKey: kAutoOpenKey) {
            openWorkbenchWhenReady(attemptsLeft: 20)
        }
    }

    // MARK: 轮询生命周期

    private func startTimer() {
        guard timer == nil else { return }
        let t = Timer(timeInterval: 15.0, repeats: true) { [weak self] _ in self?.probe() }
        // 容差让系统把本定时器与其它唤醒合并，显著降低空闲功耗。
        t.tolerance = 5.0
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    /// 休眠与锁屏期间没有观众，停掉轮询；唤醒后立刻补一次探测。
    private func observeSleepWake() {
        let workspaceCenter = NSWorkspace.shared.notificationCenter
        workspaceCenter.addObserver(forName: NSWorkspace.willSleepNotification,
                                    object: nil, queue: .main) { [weak self] _ in self?.stopTimer() }
        workspaceCenter.addObserver(forName: NSWorkspace.didWakeNotification,
                                    object: nil, queue: .main) { [weak self] _ in
            self?.startTimer()
            self?.probe()
        }
        let sessionCenter = DistributedNotificationCenter.default()
        sessionCenter.addObserver(forName: .init("com.apple.screenIsLocked"),
                                  object: nil, queue: .main) { [weak self] _ in self?.stopTimer() }
        sessionCenter.addObserver(forName: .init("com.apple.screenIsUnlocked"),
                                  object: nil, queue: .main) { [weak self] _ in
            self?.startTimer()
            self?.probe()
        }
    }

    /// 同步健康探测，仅用于「接管前的占用检查」，不进入轮询路径。
    private func backendAlreadyServing() -> Bool {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 1.0
        let semaphore = DispatchSemaphore(value: 0)
        var ok = false
        session.dataTask(with: request) { _, response, _ in
            ok = (response as? HTTPURLResponse)?.statusCode == 200
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 1.5)
        return ok
    }

    /// 健康探测：一次 localhost GET，无子进程派生。
    private func probe() {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 1.5
        session.dataTask(with: request) { [weak self] data, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            var counts: [String: Int] = [:]
            var version: String?
            var latest: String?
            var available = false
            var releaseURL: String?
            if ok, let data,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let tasks = object["tasks"] as? [String: Any] {
                    for (key, value) in tasks {
                        if let number = value as? Int { counts[key] = number }
                    }
                }
                version = object["version"] as? String
                latest = object["latest_version"] as? String
                available = object["update_available"] as? Bool ?? false
                releaseURL = object["release_url"] as? String
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.taskCounts = counts
                self.backendVersion = version
                self.latestVersion = latest
                self.updateAvailable = available
                self.releaseURL = releaseURL
                self.render(running: ok)
                if ok { self.sampleUsage() }
            }
        }.resume()
    }

    // MARK: 资源采样（proc_pid_rusage 是系统调用，不派生子进程）

    private func rusage(_ pid: pid_t) -> (rss: UInt64, cpuNanos: UInt64)? {
        var info = rusage_info_v4()
        let result = withUnsafeMutablePointer(to: &info) { pointer -> Int32 in
            pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_V4, $0)
            }
        }
        guard result == 0 else { return nil }
        return (info.ri_resident_size, info.ri_user_time + info.ri_system_time)
    }

    /// 后端 pid 只在缓存失效时才问一次 launchd，避免每轮采样都派生进程。
    private func backendPID() -> pid_t? {
        if let pid = cachedBackendPID, kill(pid, 0) == 0 { return pid }
        cachedBackendPID = nil
        let output = launchctl(["print", "\(guiDomain)/\(kBackendLabel)"]).output
        guard let marker = output.range(of: "pid = ") else { return nil }
        let digits = output[marker.upperBound...].prefix { $0.isNumber }
        guard let pid = pid_t(digits) else { return nil }
        cachedBackendPID = pid
        return pid
    }

    private func sampleUsage() {
        update(&selfUsage, pid: getpid())
        if let pid = backendPID() {
            update(&backendUsage, pid: pid)
        } else {
            backendUsage = Usage()
        }
    }

    private func update(_ usage: inout Usage, pid: pid_t) {
        guard let sample = rusage(pid) else { usage = Usage(); return }
        let now = ProcessInfo.processInfo.systemUptime
        if usage.lastAt > 0, now > usage.lastAt, sample.cpuNanos >= usage.lastCPUNanos {
            let cpuSeconds = Double(sample.cpuNanos - usage.lastCPUNanos) / 1e9
            usage.percent = cpuSeconds / (now - usage.lastAt) * 100
        }
        usage.rss = sample.rss
        usage.lastCPUNanos = sample.cpuNanos
        usage.lastAt = now
    }

    private func render(running: Bool) {
        isRunning = running
        guard let button = statusItem.button else { return }
        let symbol = running ? "sailboat.fill" : "sailboat"
        if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Bosun") {
            image.isTemplate = true  // 跟随浅色/深色菜单栏自动反色
            button.image = image
            button.title = ""
        } else {
            button.image = nil
            button.title = running ? "⚓︎" : "⚓"
        }
        button.toolTip = running ? "Bosun · 运行中 · :\(BosunConfig.port)" : "Bosun · 已停止"
    }

    // MARK: 菜单

    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        probe()  // 打开菜单时即时刷新，弥补 15s 的轮询间隔
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        let versionSuffix = backendVersion.map { " v\($0)" } ?? ""
        let status = NSMenuItem(title: isRunning ? "Bosun\(versionSuffix) · 运行中 · :\(BosunConfig.port)" : "Bosun · 已停止",
                                action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)
        if isRunning {
            menu.addItem(infoItem(taskSummary()))
            menu.addItem(infoItem("后端 \(memoryText(backendUsage.rss)) · CPU \(percentText(backendUsage.percent))"))
            menu.addItem(infoItem("图标 \(memoryText(selfUsage.rss)) · CPU \(percentText(selfUsage.percent))"))
            if updateAvailable, let latest = latestVersion {
                addItem(to: menu, title: "发现新版本 v\(latest) — 查看发布页", action: #selector(openReleasePage))
            }
        }
        menu.addItem(.separator())

        let open = NSMenuItem(title: "打开工作台", action: #selector(openWorkbench), keyEquivalent: "o")
        open.target = self
        open.isEnabled = isRunning
        menu.addItem(open)

        if isRunning {
            addItem(to: menu, title: "重启后端", action: #selector(restartBackend))
            addItem(to: menu, title: "停止后端", action: #selector(stopBackend))
        } else {
            addItem(to: menu, title: "启动后端", action: #selector(startBackend))
        }

        menu.addItem(.separator())
        let logItem = NSMenuItem(title: "查看日志", action: nil, keyEquivalent: "")
        let logMenu = NSMenu()
        logMenu.addItem(logEntry(title: "运行日志 (stdout)", url: outLogURL, action: #selector(openOutLog)))
        logMenu.addItem(logEntry(title: "错误日志 (stderr)", url: errLogURL, action: #selector(openErrLog)))
        logMenu.addItem(.separator())
        let reveal = NSMenuItem(title: "在访达中显示", action: #selector(revealLogs), keyEquivalent: "")
        reveal.target = self
        logMenu.addItem(reveal)
        logItem.submenu = logMenu
        menu.addItem(logItem)

        let autoOpen = NSMenuItem(title: "启动时打开工作台", action: #selector(toggleAutoOpen), keyEquivalent: "")
        autoOpen.target = self
        autoOpen.state = UserDefaults.standard.bool(forKey: kAutoOpenKey) ? .on : .off
        menu.addItem(autoOpen)

        let autostart = NSMenuItem(title: "开机自启", action: #selector(toggleAutostart), keyEquivalent: "")
        autostart.target = self
        autostart.state = isAutostartEnabled() ? .on : .off
        menu.addItem(autostart)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出（后端继续运行）", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
    }

    /// 纯展示行：不可点击，只呈现状态。
    private func infoItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func memoryText(_ bytes: UInt64) -> String {
        guard bytes > 0 else { return "—" }
        return ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .memory)
    }

    private func percentText(_ value: Double) -> String {
        String(format: "%.1f%%", max(0, value))
    }

    private func taskSummary() -> String {
        let running = taskCounts["running"] ?? 0
        let waiting = taskCounts["waiting_input"] ?? 0
        let queued = taskCounts["queued"] ?? 0
        var parts = ["任务 \(running) 运行中"]
        if waiting > 0 { parts.append("\(waiting) 等待输入") }
        if queued > 0 { parts.append("\(queued) 排队") }
        return parts.joined(separator: " · ")
    }

    /// 菜单项直接标出体积，空文件一眼可见，不用点进去才发现没内容。
    private func logEntry(title: String, url: URL, action: Selector) -> NSMenuItem {
        let size = fileSize(url)
        let suffix = size > 0 ? ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file) : "空"
        let item = NSMenuItem(title: "\(title) — \(suffix)", action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    @objc private func revealLogs() {
        for path in [outLogURL, errLogURL] where !fm.fileExists(atPath: path.path) {
            fm.createFile(atPath: path.path, contents: Data())
        }
        NSWorkspace.shared.activateFileViewerSelecting([outLogURL, errLogURL])
    }

    private func addItem(to menu: NSMenu, title: String, action: Selector) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
    }

    // MARK: 动作

    @objc private func openWorkbench() {
        openWorkbenchApp()
    }

    /// 优先拉起已安装的 PWA（窗口形态与登录态和手工点开完全一致），
    /// 找不到才退回默认浏览器开地址。
    private func workbenchAppURL() -> URL? {
        if !BosunConfig.pwaBundleID.isEmpty,
           let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: BosunConfig.pwaBundleID) {
            return url
        }
        // 兜底：PWA 重装后 bundle id 可能变化，按目录名再找一次。
        let chromeApps = homeDir.appendingPathComponent("Applications/Chrome Apps.localized")
        let entries = (try? fm.contentsOfDirectory(at: chromeApps, includingPropertiesForKeys: nil)) ?? []
        return entries.first {
            $0.pathExtension == "app" && $0.deletingPathExtension().lastPathComponent.contains("Bosun")
        }
    }

    private func openWorkbenchApp() {
        guard let app = workbenchAppURL() else {
            NSWorkspace.shared.open(workbenchURL)
            return
        }
        NSWorkspace.shared.openApplication(at: app, configuration: NSWorkspace.OpenConfiguration())
    }

    /// 打开发布页看更新说明；实际更新在工作台「设置 → 检查更新」里做。
    @objc private func openReleasePage() {
        let fallback = "https://github.com/thsrite/bosun/releases"
        guard let url = URL(string: releaseURL ?? fallback) ?? URL(string: fallback) else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func toggleAutoOpen() {
        let defaults = UserDefaults.standard
        defaults.set(!defaults.bool(forKey: kAutoOpenKey), forKey: kAutoOpenKey)
    }

    /// 后端就绪前打开只会看到白屏，这里按 1 秒一次轮询等它起来，最多等 20 秒。
    private func openWorkbenchWhenReady(attemptsLeft: Int) {
        guard attemptsLeft > 0, !didAutoOpen else { return }
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 1.5
        session.dataTask(with: request) { [weak self] _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                guard let self, !self.didAutoOpen else { return }
                if ok {
                    self.didAutoOpen = true
                    self.render(running: true)
                    self.openWorkbenchApp()
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                        self.openWorkbenchWhenReady(attemptsLeft: attemptsLeft - 1)
                    }
                }
            }
        }.resume()
    }

    /// 运行日志（stdout）与错误日志（stderr）分别打开。
    /// 注意 uvicorn 默认把启动信息和访问日志写到 stderr，所以两个文件都可能有正常内容，
    /// 不能只开其中一个。
    @objc private func openOutLog() {
        openLogFile(outLogURL)
    }

    @objc private func openErrLog() {
        openLogFile(errLogURL)
    }

    private func openLogFile(_ url: URL) {
        for path in [outLogURL, errLogURL] where !fm.fileExists(atPath: path.path) {
            fm.createFile(atPath: path.path, contents: Data())
        }
        // 日志为空通常不是故障，而是后端根本不由 launchd 启动（例如手工跑了 start.sh）。
        // 直接打开空白文件等于什么都没发生，这里必须把原因讲清楚。
        guard fileSize(url) > 0 else {
            reportEmptyLog()
            return
        }
        openInConsole(url)
    }

    private func fileSize(_ url: URL) -> Int {
        guard let attributes = try? fm.attributesOfItem(atPath: url.path),
              let size = attributes[.size] as? Int else { return 0 }
        return size
    }

    /// 显式交给「控制台」打开，它能实时跟随文件追加，比丢给未知的默认关联应用可靠。
    private func openInConsole(_ url: URL) {
        let console = URL(fileURLWithPath: "/System/Applications/Utilities/Console.app")
        if fm.fileExists(atPath: console.path) {
            NSWorkspace.shared.open([url], withApplicationAt: console,
                                    configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.open(url)
        }
    }

    private func reportEmptyLog() {
        NSApp.activate(ignoringOtherApps: true)  // LSUIElement 程序需主动抢焦点，否则弹窗藏在后面
        let alert = NSAlert()
        alert.messageText = "暂时没有日志内容"
        alert.informativeText = """
            launchd 日志为空，说明当前后端不是由 Bosun.app 启动的\
            （例如你在终端里手工跑了 start.sh，它的输出留在那个终端窗口里）。

            改用菜单里的「启动后端」拉起后，输出才会写入：
            \(errLogURL.path)
            """
        alert.addButton(withTitle: "好")
        alert.addButton(withTitle: "在访达中显示")
        if alert.runModal() == .alertSecondButtonReturn {
            NSWorkspace.shared.activateFileViewerSelecting([errLogURL, outLogURL])
        }
    }

    @objc private func startBackend() {
        ensureBackendPlistExists()
        // 端口已被别人（例如手工跑的 start.sh）占着就不要接管：
        // 否则 uvicorn 绑不上端口即退出，被 KeepAlive 反复拉起形成重启风暴。
        guard !backendAlreadyServing() else {
            refreshSoon()
            return
        }
        bootstrapAndStart()
        refreshSoon()
    }

    /// RunAtLoad 默认为 false，所以 bootstrap 之后必须显式 kickstart 才会真正跑起来。
    private func bootstrapAndStart() {
        launchctl(["bootstrap", guiDomain, backendPlistURL.path])
        launchctl(["kickstart", "\(guiDomain)/\(kBackendLabel)"])
    }

    @objc private func stopBackend() {
        launchctl(["bootout", "\(guiDomain)/\(kBackendLabel)"])
        refreshSoon()
    }

    @objc private func restartBackend() {
        // kickstart -k 会先杀后起；服务未加载时回退到 bootstrap。
        let result = launchctl(["kickstart", "-k", "\(guiDomain)/\(kBackendLabel)"])
        if result.code != 0 {
            bootstrapAndStart()
        }
        refreshSoon()
    }

    @objc private func toggleAutostart() {
        let enabled = isAutostartEnabled()
        // 只改磁盘上的 plist，不重新 bootstrap：当前会话不受影响，下次登录生效。
        try? writePlist(backendPlistDict(runAtLoad: !enabled), to: backendPlistURL)
        if enabled {
            try? fm.removeItem(at: menubarPlistURL)
        } else {
            try? writePlist(menubarPlistDict(), to: menubarPlistURL)
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    /// 操作后给 launchd 一点落地时间再刷新状态，避免立刻探测到中间态。
    private func refreshSoon() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in self?.probe() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) { [weak self] in self?.probe() }
    }

    // MARK: 状态查询

    private func isAutostartEnabled() -> Bool {
        guard let dict = readPlist(backendPlistURL),
              let runAtLoad = dict["RunAtLoad"] as? Bool else { return false }
        return runAtLoad && fm.fileExists(atPath: menubarPlistURL.path)
    }

    /// 默认**不开机自启**：只落一份 RunAtLoad=false 的后端定义，不写图标自启 plist。
    /// 想要开机自启，由用户在菜单里主动勾选。
    private func ensureBackendPlistExists() {
        guard !fm.fileExists(atPath: backendPlistURL.path) else { return }
        try? writePlist(backendPlistDict(runAtLoad: false), to: backendPlistURL)
    }

    /// 首次启动时若后端尚未被 launchd 接管，就地接管一次（幂等）。
    /// 两道闸门缺一不可：已被 launchd 管着就不重复 bootstrap；端口被外部进程
    /// （手工 start.sh）占着也不接管，否则会陷入「绑定失败→KeepAlive 拉起」的重启风暴。
    private func startBackendIfNeeded() {
        guard launchctl(["print", "\(guiDomain)/\(kBackendLabel)"]).code != 0 else { return }
        guard !backendAlreadyServing() else { return }
        bootstrapAndStart()
    }
}

// MARK: - 入口

// 单实例保护：避免菜单栏出现两个图标、两份轮询。
if let bundleID = Bundle.main.bundleIdentifier,
   NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).count > 1 {
    exit(0)
}

let application = NSApplication.shared
let controller = BosunController()
application.delegate = controller
application.setActivationPolicy(.accessory)  // 无 Dock 图标、无主窗口
application.run()
