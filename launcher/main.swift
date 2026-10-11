// droidrt launcher: the CFBundleExecutable of every generated .app.
//
// It reads its own Info.plist, starts `<DroidrtCommand...> launch <pkg> --title <name>`
// as a child process, shows up in the Dock as a regular app with the bundle icon and
// brings the scrcpy window to the front once it appears. Quitting the launcher stops
// the window with SIGTERM; the launcher exits when the child exits.

import AppKit
import Darwin

// MARK: - Process table helpers

struct ProcInfo {
    let pid: pid_t
    let ppid: pid_t
    let name: String
}

/// Snapshot of every process visible to the current user.
func processTable() -> [ProcInfo] {
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
    var size = 0
    guard sysctl(&mib, UInt32(mib.count), nil, &size, nil, 0) == 0, size > 0 else { return [] }
    // The table can grow between the two calls, so leave some headroom.
    size += size / 8
    let stride = MemoryLayout<kinfo_proc>.stride
    var procs = [kinfo_proc](repeating: kinfo_proc(), count: size / stride)
    guard sysctl(&mib, UInt32(mib.count), &procs, &size, nil, 0) == 0 else { return [] }
    let count = size / stride
    return procs.prefix(count).map { kp in
        var comm = kp.kp_proc.p_comm
        let name = withUnsafeBytes(of: &comm) { raw -> String in
            let bytes = raw.prefix { $0 != 0 }
            return String(decoding: bytes, as: UTF8.self)
        }
        return ProcInfo(pid: kp.kp_proc.p_pid, ppid: kp.kp_eproc.e_ppid, name: name)
    }
}

/// All descendants of `root` (not including `root`), parents before children.
func descendants(of root: pid_t) -> [ProcInfo] {
    let table = processTable()
    var byParent: [pid_t: [ProcInfo]] = [:]
    for p in table { byParent[p.ppid, default: []].append(p) }
    var result: [ProcInfo] = []
    var queue = [root]
    while !queue.isEmpty {
        let pid = queue.removeFirst()
        for child in byParent[pid] ?? [] where child.pid != pid {
            result.append(child)
            queue.append(child.pid)
        }
    }
    return result
}

// MARK: - Window lookup

struct WindowMatch {
    let pid: pid_t
    let windowID: CGWindowID
}

/// First normal (layer 0) on-screen window owned by any of `pids`, scrcpy windows first.
func findWindow(ownedBy pids: Set<pid_t>, preferring preferred: Set<pid_t>) -> WindowMatch? {
    guard !pids.isEmpty,
          let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                kCGNullWindowID) as? [[String: Any]]
    else { return nil }
    var fallback: WindowMatch?
    for entry in list {
        guard let owner = (entry[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
              pids.contains(owner),
              (entry[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
              let number = (entry[kCGWindowNumber as String] as? NSNumber)?.uint32Value
        else { continue }
        let match = WindowMatch(pid: owner, windowID: number)
        if preferred.contains(owner) { return match }
        if fallback == nil { fallback = match }
    }
    return fallback
}

/// Window of an already running scrcpy instance whose title matches `title`.
/// Reading window titles may need the Screen Recording permission; without it the
/// title is missing and this simply returns nil.
func findScrcpyWindow(titled title: String) -> WindowMatch? {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                kCGNullWindowID) as? [[String: Any]]
    else { return nil }
    for entry in list {
        guard (entry[kCGWindowOwnerName as String] as? String) == "scrcpy",
              (entry[kCGWindowName as String] as? String) == title,
              let owner = (entry[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
              let number = (entry[kCGWindowNumber as String] as? NSNumber)?.uint32Value
        else { continue }
        return WindowMatch(pid: owner, windowID: number)
    }
    return nil
}

// MARK: - Logging

final class LauncherLog {
    let handle: FileHandle?

    init(package: String) {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/droidrt", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("\(package).launcher.log")
        if !FileManager.default.fileExists(atPath: file.path) {
            FileManager.default.createFile(atPath: file.path, contents: nil)
        }
        handle = try? FileHandle(forWritingTo: file)
        _ = try? handle?.seekToEnd()
    }

    func write(_ message: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let line = "[\(stamp)] launcher: \(message)\n"
        if let handle {
            try? handle.write(contentsOf: Data(line.utf8))
        } else {
            FileHandle.standardError.write(Data(line.utf8))
        }
    }
}

// MARK: - App delegate

final class LauncherDelegate: NSObject, NSApplicationDelegate {
    private let package: String
    private let title: String
    private let command: [String]
    private let log: LauncherLog

    private var child: Process?
    private var pollTimer: Timer?
    private var pollDeadline = Date()
    private var shownWindow = false
    private var terminating = false
    private var pendingTerminateReply = false
    private var exitStatus: Int32 = 0
    private var signalSources: [DispatchSourceSignal] = []

    init(package: String, title: String, command: [String], log: LauncherLog) {
        self.package = package
        self.title = title
        self.command = command
        self.log = log
    }

    func applicationWillFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.mainMenu = makeMainMenu()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installSignalHandlers()
        startChild()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !bringWindowToFront() {
            // The window may not exist yet (VM still booting); keep looking for a while.
            startPolling(for: 60)
        }
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let child, child.isRunning else { return .terminateNow }
        if !pendingTerminateReply {
            pendingTerminateReply = true
            stopChild()
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        log.write("exit (status \(exitStatus))")
    }

    // MARK: Child process

    private func startChild() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command[0])
        process.arguments = Array(command.dropFirst()) + ["launch", package, "--title", title]
        process.standardInput = FileHandle.nullDevice
        if let handle = log.handle {
            process.standardOutput = handle
            process.standardError = handle
        }
        process.terminationHandler = { [weak self] proc in
            let status = proc.terminationStatus
            let reason = proc.terminationReason == .uncaughtSignal ? "signal" : "exit"
            DispatchQueue.main.async { self?.childExited(status: status, reason: reason) }
        }
        log.write("starting: \(([command[0]] + (process.arguments ?? [])).joined(separator: " "))")
        do {
            try process.run()
        } catch {
            log.write("failed to start child: \(error.localizedDescription)")
            exitStatus = 1
            NSApp.terminate(nil)
            return
        }
        child = process
        log.write("child pid \(process.processIdentifier)")
        // A cold VM boot takes up to about 30 s before scrcpy opens its window.
        startPolling(for: 180)
    }

    private func childExited(status: Int32, reason: String) {
        log.write("child finished (\(reason) \(status))")
        exitStatus = reason == "signal" ? 128 + status : status
        stopPolling()
        // `launch` returns right away when the app is already open in another window:
        // in that case, bring the existing window forward before leaving.
        if !shownWindow && !terminating, let existing = findScrcpyWindow(titled: title) {
            activate(pid: existing.pid)
        }
        if pendingTerminateReply {
            pendingTerminateReply = false
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            NSApp.terminate(nil)
        }
    }

    /// Sends SIGTERM to the scrcpy window first so the Python wrapper can finish its
    /// own cleanup (idle VM shutdown); falls back to the direct child. Never SIGKILL.
    private func stopChild() {
        guard let child, child.isRunning else { return }
        terminating = true
        let tree = descendants(of: child.processIdentifier)
        let windows = tree.filter { $0.name == "scrcpy" }
        if windows.isEmpty {
            log.write("SIGTERM -> child \(child.processIdentifier)")
            kill(child.processIdentifier, SIGTERM)
        } else {
            for proc in windows {
                log.write("SIGTERM -> scrcpy \(proc.pid)")
                kill(proc.pid, SIGTERM)
            }
            // If the wrapper is still around after a grace period, ask it to stop too.
            let pid = child.processIdentifier
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
                guard let self, let child = self.child, child.isRunning,
                      child.processIdentifier == pid else { return }
                self.log.write("SIGTERM -> child \(pid) (grace period over)")
                kill(pid, SIGTERM)
            }
        }
    }

    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                guard let self else { return }
                self.log.write("received signal \(sig)")
                if let child = self.child, child.isRunning {
                    self.stopChild()
                } else {
                    NSApp.terminate(nil)
                }
            }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: Window activation

    private func startPolling(for seconds: TimeInterval) {
        pollDeadline = Date().addingTimeInterval(seconds)
        guard pollTimer == nil else { return }
        let timer = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in
            guard let self else { return }
            if self.bringWindowToFront() || Date() > self.pollDeadline {
                self.stopPolling()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
    }

    private func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    /// Activates the app owning the child's window. Returns false if there is no window yet.
    @discardableResult
    private func bringWindowToFront() -> Bool {
        guard let child, child.isRunning else { return false }
        let tree = descendants(of: child.processIdentifier)
        let pids = Set(tree.map(\.pid)).union([child.processIdentifier])
        let scrcpy = Set(tree.filter { $0.name == "scrcpy" }.map(\.pid))
        guard let match = findWindow(ownedBy: pids, preferring: scrcpy) else { return false }
        if !shownWindow { log.write("window \(match.windowID) owned by pid \(match.pid)") }
        shownWindow = true
        return activate(pid: match.pid)
    }

    @discardableResult
    private func activate(pid: pid_t) -> Bool {
        guard let app = NSRunningApplication(processIdentifier: pid) else { return false }
        if #available(macOS 14.0, *) {
            // Cooperative activation: hand our activation over to the window's process.
            NSApp.yieldActivation(to: app)
            return app.activate(options: [.activateAllWindows])
        } else {
            return app.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        }
    }

    // MARK: Menu

    private func makeMainMenu() -> NSMenu {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu(title: title)
        appMenu.addItem(withTitle: "Show Window", action: #selector(showWindow(_:)), keyEquivalent: "")
            .target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide \(title)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit \(title)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        return main
    }

    @objc private func showWindow(_ sender: Any?) {
        if !bringWindowToFront() { startPolling(for: 60) }
    }
}

// MARK: - Entry point

let info = Bundle.main.infoDictionary ?? [:]
let package = info["DroidrtPackage"] as? String ?? ""
let title = (info["CFBundleName"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? package
let command = info["DroidrtCommand"] as? [String] ?? []
let launcherLog = LauncherLog(package: package.isEmpty ? "unknown" : package)

guard !package.isEmpty, !command.isEmpty, !command[0].isEmpty else {
    launcherLog.write("Info.plist is missing DroidrtPackage or DroidrtCommand")
    exit(78) // EX_CONFIG
}

let app = NSApplication.shared
let delegate = LauncherDelegate(package: package, title: title, command: command, log: launcherLog)
app.delegate = delegate
app.run()
exit(0)
