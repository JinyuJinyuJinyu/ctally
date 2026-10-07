// CTally — a floating status light for Claude Code sessions: whose turn is it?
//
// Install: ./install.sh   (builds this file, installs CTally.app and the hooks)
// Build:   swiftc -O ctally.swift -o ctally
//
// Reads ~/.claude/ctally.d/<session-id>, one file per Claude Code session, each
// holding "<word> <pid> <project name>" written by hooks/ctally.sh:
//   working | done | waiting | idle   (anything else, or an empty directory, means idle)
//
// Up to three sessions show as big badges; four or more as a compact list, one row per
// session with its name (from /rename, or the title Claude gave it) over its directory.
// The chevron on the list's count bar folds it down to just the counts.
// Click a row (or a badge) to bring that session's terminal to the front.
//
// Switch it off and on from the hexagon icon in the menu bar, the settings window, or the
// right-click menu; the settings window also sets its opacity. Off is remembered, and
// while off the app sits dormant: only the menu bar icon (dimmed) stays on screen, and no
// timers run.
//
// Deliberately silent: no sound, no notifications, no terminal output.

import Cocoa
import QuartzCore

// MARK: - State

enum SessionState: String {
    case working
    case waiting
    case done
    case idle

    /// Most urgent first: the order counts are listed in.
    static let byUrgency: [SessionState] = [.waiting, .working, .done, .idle]

    /// How loudly this state asks for me; decides who survives the row cap.
    var urgency: Int {
        switch self {
        case .waiting: return 3   // blocked on me; that session cannot proceed
        case .done:    return 2   // finished, wants my next instruction
        case .working: return 1   // busy, needs nothing
        case .idle:    return 0
        }
    }

    var accent: NSColor {
        switch self {
        case .working: return NSColor(srgbRed: 0.29, green: 0.80, blue: 0.95, alpha: 1)  // cyan
        case .waiting: return NSColor(srgbRed: 1.00, green: 0.70, blue: 0.16, alpha: 1)  // amber
        case .done:    return NSColor(srgbRed: 0.27, green: 0.88, blue: 0.53, alpha: 1)  // green
        case .idle:    return NSColor(srgbRed: 0.60, green: 0.65, blue: 0.74, alpha: 1)  // slate
        }
    }
}

/// One session as the view needs to see it.
struct SessionSnapshot: Equatable {
    let id: String
    let state: SessionState
    let label: String          // project folder name, from the hook
    var pid: pid_t = 0         // the claude process, from the hook
    var title: String? = nil   // session name, from the transcript
    var path: String? = nil    // directory the session started in, from the transcript
}

/// CTally's shape: a hexagon standing on a point, shared by the badges, the list's marks and
/// the menu bar icon.
enum Hexagon {
    static func points(center: CGPoint, radius: CGFloat) -> [CGPoint] {
        (0..<6).map { i in
            let angle = CGFloat.pi / 180 * (30 + 60 * CGFloat(i))
            return CGPoint(x: center.x + radius * cos(angle), y: center.y + radius * sin(angle))
        }
    }

    static func path(center: CGPoint, radius: CGFloat) -> NSBezierPath {
        let pts = points(center: center, radius: radius)
        let path = NSBezierPath()
        path.move(to: pts[0])
        for p in pts.dropFirst() { path.line(to: p) }
        path.close()
        return path
    }
}

// MARK: - Session names

/// Names a session the way Claude Code's own session list does: the title I gave it with
/// /rename, else the one Claude generated, plus the directory it started in. Both live in
/// the session's transcript, which can run to hundreds of megabytes, so only its ends are
/// read: the start once for the directory, the last stretch for the title when it moves.
final class SessionInfoReader {

    private struct Entry {
        var transcript: URL?
        var path: String?
        var custom: String?
        var generated: String?
        var modified = Date.distantPast
        var lastLook = Date.distantPast
    }

    private static let window = 256 * 1024
    private static let quote = UInt8(ascii: "\"")
    private static let backslash = UInt8(ascii: "\\")

    private let projects: URL
    private var entries: [String: Entry] = [:]

    init(projects: URL) {
        self.projects = projects
    }

    /// Cheap to call every poll: the transcript is looked at every few seconds at most,
    /// and read only when it has changed since.
    func info(for id: String) -> (title: String?, path: String?) {
        var entry = entries[id] ?? Entry()
        // Claude names a session after its first prompt, so look harder until it has.
        let every: TimeInterval = (entry.custom ?? entry.generated) == nil ? 2 : 5
        let now = Date()
        if now.timeIntervalSince(entry.lastLook) >= every {
            entry.lastLook = now
            refresh(&entry, id: id)
        }
        entries[id] = entry
        return (entry.custom ?? entry.generated, entry.path)
    }

    func forget(allBut ids: Set<String>) {
        entries = entries.filter { ids.contains($0.key) }
    }

    private func refresh(_ entry: inout Entry, id: String) {
        if entry.transcript == nil { entry.transcript = locate(id) }
        guard let url = entry.transcript,
              let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let modified = attrs[.modificationDate] as? Date, modified != entry.modified,
              let handle = try? FileHandle(forReadingFrom: url) else { return }
        defer { try? handle.close() }
        entry.modified = modified

        let window = SessionInfoReader.window
        if entry.path == nil, let head = try? handle.read(upToCount: window) {
            entry.path = cwd(in: head, last: false)
        }
        guard let size = try? handle.seekToEnd(),
              (try? handle.seek(toOffset: size > UInt64(window) ? size - UInt64(window) : 0)) != nil,
              let tail = try? handle.readToEnd() else { return }
        if entry.path == nil { entry.path = cwd(in: tail, last: true) }

        // Newest first; the first line of the window may be cut, and simply won't parse.
        var custom: String?, generated: String?
        for line in tail.split(separator: UInt8(ascii: "\n")).reversed() {
            if custom != nil && generated != nil { break }
            guard line.count < 4096 else { continue }       // title records are tiny
            let text = String(decoding: line, as: UTF8.self)
            guard text.contains("-title\""),
                  let record = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
            else { continue }
            switch record["type"] as? String {
            case "custom-title" where custom == nil: custom = record["customTitle"] as? String
            case "ai-title" where generated == nil: generated = record["aiTitle"] as? String
            default: break
            }
        }
        // A window without a title record keeps what an earlier read found.
        if let custom = custom, !custom.isEmpty { entry.custom = custom }
        if let generated = generated, !generated.isEmpty { entry.generated = generated }
    }

    /// Transcripts sit in ~/.claude/projects/<encoded directory>/<session id>.jsonl.
    private func locate(_ id: String) -> URL? {
        let fm = FileManager.default
        let dirs = (try? fm.contentsOfDirectory(at: projects, includingPropertiesForKeys: nil)) ?? []
        return dirs.lazy
            .map { $0.appendingPathComponent(id + ".jsonl") }
            .first { fm.fileExists(atPath: $0.path) }
    }

    /// The "cwd" field of a transcript record, found by bytes rather than by parsing
    /// lines that can each be megabytes long.
    private func cwd(in data: Data, last: Bool) -> String? {
        let key = Data("\"cwd\"".utf8)
        var scope = data.startIndex..<data.endIndex
        while let found = data.range(of: key, options: last ? .backwards : [], in: scope) {
            scope = last ? data.startIndex..<found.lowerBound : found.upperBound..<data.endIndex
            // The key, then `:` with any spacing around it, then the string.
            var start = found.upperBound
            while start < data.endIndex, data[start] == UInt8(ascii: " ") { start += 1 }
            guard start < data.endIndex, data[start] == UInt8(ascii: ":") else { continue }
            start += 1
            while start < data.endIndex, data[start] == UInt8(ascii: " ") { start += 1 }
            guard start < data.endIndex, data[start] == SessionInfoReader.quote else { continue }

            var end = start + 1
            while end < data.endIndex, data[end] != SessionInfoReader.quote {
                end += data[end] == SessionInfoReader.backslash ? 2 : 1
            }
            guard end < data.endIndex else { return nil }
            let literal = Data(data[start...end])                 // the string, both quotes included
            if let path = try? JSONSerialization.jsonObject(with: literal, options: .fragmentsAllowed) as? String,
               !path.isEmpty {
                return path
            }
        }
        return nil
    }
}

// MARK: - State directory

/// Watches one state file per session.
final class StateReader {

    private struct Session {
        let state: SessionState
        let pid: pid_t
        let label: String
        let modified: Date
        let size: Int

        /// A session whose process is gone can no longer be doing anything.
        var isAlive: Bool {
            if pid <= 0 { return true }        // no pid recorded, can't disprove it
            if kill(pid, 0) == 0 { return true }
            return errno == EPERM              // alive, just not ours to signal
        }
    }

    private static let staleAfter: TimeInterval = 24 * 60 * 60

    private let dir: URL
    private let namer: SessionInfoReader
    private var cache: [String: Session] = [:]
    private var firstSeen: [String: Date] = [:]
    private var lastPrune = Date.distantPast

    init(directory: URL, transcripts: URL) {
        self.dir = directory
        self.namer = SessionInfoReader(projects: transcripts)
    }

    func poll() -> [SessionSnapshot] {
        let fm = FileManager.default
        let names = Set((try? fm.contentsOfDirectory(atPath: dir.path))?
            .filter { !$0.hasPrefix(".") } ?? [])
        // One timestamp for the whole sweep: sessions found together tie, and the
        // tie-break on name keeps their order stable instead of set-iteration order.
        let sweep = Date()

        for name in names {
            let path = dir.appendingPathComponent(name).path
            let attrs = try? fm.attributesOfItem(atPath: path)
            let modified = (attrs?[.modificationDate] as? Date) ?? .distantPast
            let size = (attrs?[.size] as? Int) ?? 0
            // Stat first: only re-read a file that actually moved.
            if let known = cache[name], known.modified == modified, known.size == size { continue }
            cache[name] = read(path, modified: modified, size: size)
            if firstSeen[name] == nil { firstSeen[name] = sweep }
        }
        cache = cache.filter { names.contains($0.key) }
        firstSeen = firstSeen.filter { names.contains($0.key) }

        prune()
        namer.forget(allBut: Set(cache.keys))

        // Oldest session first, so rows never shuffle under me.
        let ordered = cache.keys.sorted {
            let l = firstSeen[$0] ?? .distantPast, r = firstSeen[$1] ?? .distantPast
            return l == r ? $0 < $1 : l < r
        }

        let live = ordered.filter { cache[$0]?.isAlive == true }
        if !live.isEmpty { return live.map(snapshot) }

        // Nothing is running. A finished turn still deserves to be on screen when I
        // come back; a dead session's "working" means only that it was killed.
        let finished = ordered.filter { cache[$0]?.state == .done }
        return finished.map(snapshot)
    }

    private func snapshot(_ name: String) -> SessionSnapshot {
        let session = cache[name]
        let info = namer.info(for: name)
        return SessionSnapshot(id: name,
                               state: session?.state ?? .idle,
                               label: session?.label ?? "",
                               pid: session?.pid ?? 0,
                               title: info.title,
                               path: info.path)
    }

    private func read(_ path: String, modified: Date, size: Int) -> Session? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let raw = String(data: data, encoding: .utf8) else { return nil }
        let fields = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            .split(whereSeparator: { $0 == " " || $0 == "\t" })
        guard let word = fields.first else { return nil }
        // "<word> <pid> <project name>" — the name may itself contain spaces.
        return Session(state: SessionState(rawValue: String(word).lowercased()) ?? .idle,
                       pid: fields.count > 1 ? (pid_t(fields[1]) ?? 0) : 0,
                       label: fields.count > 2 ? fields.dropFirst(2).joined(separator: " ") : "",
                       modified: modified,
                       size: size)
    }

    /// Sessions killed without a SessionEnd hook leave their file behind.
    private func prune() {
        let now = Date()
        guard now.timeIntervalSince(lastPrune) > 60 else { return }
        lastPrune = now
        for (name, session) in cache
        where !session.isAlive && now.timeIntervalSince(session.modified) > StateReader.staleAfter {
            try? FileManager.default.removeItem(at: dir.appendingPathComponent(name))
            cache[name] = nil
            firstSeen[name] = nil
        }
    }
}

// MARK: - Bringing a session forward

/// Brings a session's terminal to the front. Inside tmux, that means switching tmux to the
/// session's pane, then raising the Terminal tab attached to that tmux session (or opening
/// one if none is); outside tmux, raising the tab the session runs in. The claude process
/// says which: tmux leaves TMUX (its socket) and TMUX_PANE (the exact pane) in the
/// environment of everything started inside it. All of this shells out, so it runs off the
/// main thread, and it stays silent when anything along the way is missing.
final class SessionFocuser {

    private static let terminal = "com.apple.Terminal"

    /// Picks the tab whose tty matches, and puts its window in front.
    private static let raiseTab = [
        "on run argv",
        "  set target to item 1 of argv",
        "  tell application \"Terminal\"",
        "    repeat with w in windows",
        "      repeat with t in tabs of w",
        "        if tty of t is target then",
        "          set miniaturized of w to false",
        "          set selected tab of w to t",
        "          set index of w to 1",
        "          activate",
        "          return",
        "        end if",
        "      end repeat",
        "    end repeat",
        "  end tell",
        "end run",
    ]

    private let queue = DispatchQueue(label: "ctally.focus")

    func focus(_ pid: pid_t) {
        guard pid > 0 else { return }
        queue.async { SessionFocuser.focusNow(pid) }
    }

    private static func focusNow(_ pid: pid_t) {
        guard let launch = launchInfo(of: pid) else { return }
        if let server = launch.environment["TMUX"], let pane = launch.environment["TMUX_PANE"] {
            focusTmux(pane: pane, server: server)
        } else if let tty = tty(of: pid) {
            raise(tty: tty, hostedBy: pid)
        }
    }

    private static func focusTmux(pane: String, server: String) {
        // TMUX is "<socket>,<server pid>,<session index>"; run the server's own binary.
        let parts = server.split(separator: ",").map(String.init)
        guard parts.count >= 2 else { return }
        let fromServer = pid_t(parts[1]).flatMap { launchInfo(of: $0)?.path }
        guard let binary = fromServer.flatMap({ $0.hasPrefix("/") ? $0 : nil }) ?? installedTmux() else { return }
        let socket = parts[0]
        @discardableResult func tmux(_ args: String...) -> String? { run(binary, ["-S", socket] + args) }

        tmux("select-window", "-t", pane)
        tmux("select-pane", "-t", pane)
        guard let session = tmux("display-message", "-p", "-t", pane, "#{session_name}")?
                .trimmingCharacters(in: .whitespacesAndNewlines), !session.isEmpty else { return }

        // The tab attached to that tmux session that I used most recently.
        let clients = (tmux("list-clients", "-t", "=" + session,
                            "-F", "#{client_activity} #{client_pid} #{client_tty}") ?? "")
            .split(separator: "\n")
            .compactMap { line -> (activity: Int, pid: pid_t, tty: String)? in
                let f = line.split(separator: " ", maxSplits: 2).map(String.init)
                guard f.count == 3, let activity = Int(f[0]), let pid = pid_t(f[1]) else { return nil }
                return (activity, pid, f[2])
            }
        if let client = clients.max(by: { $0.activity < $1.activity }) {
            raise(tty: client.tty, hostedBy: client.pid)
        } else {
            // Nobody is looking at that session: open a Terminal window onto it.
            let command = [binary, "-S", socket, "attach-session", "-t", "=" + session]
                .map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }
                .joined(separator: " ")
            let quoted = command.replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"")
            run("/usr/bin/osascript", ["-e", "tell application \"Terminal\"",
                                       "-e", "do script \"\(quoted)\"",
                                       "-e", "activate",
                                       "-e", "end tell"])
        }
    }

    /// Raises the tab showing `tty` in Terminal; any other app, just brought forward.
    private static func raise(tty: String, hostedBy pid: pid_t) {
        guard let app = hostApp(of: pid) else { return }
        if app.bundleIdentifier == terminal {
            run("/usr/bin/osascript", raiseTab.flatMap { ["-e", $0] } + [tty])
        } else {
            DispatchQueue.main.async { app.activate(options: [.activateIgnoringOtherApps]) }
        }
    }

    /// The nearest ancestor that is an ordinary app: Terminal, for a shell in one of its tabs.
    private static func hostApp(of pid: pid_t) -> NSRunningApplication? {
        var current = pid
        for _ in 0..<32 {
            if let app = NSRunningApplication(processIdentifier: current), app.activationPolicy == .regular {
                return app
            }
            guard let up = processInfo(current)?.kp_eproc.e_ppid, up > 1, up != current else { return nil }
            current = up
        }
        return nil
    }

    private static func installedTmux() -> String? {
        ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/opt/local/bin/tmux", "/usr/bin/tmux"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    // MARK: Processes

    private static func processInfo(_ pid: pid_t) -> kinfo_proc? {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        guard sysctl(&mib, 4, &info, &size, nil, 0) == 0, size > 0 else { return nil }
        return info
    }

    /// The controlling terminal, as Terminal names its tabs' ttys: "/dev/ttys005".
    private static func tty(of pid: pid_t) -> String? {
        guard let device = processInfo(pid)?.kp_eproc.e_tdev, device != -1,
              let name = devname(device, S_IFCHR) else { return nil }
        let tty = String(cString: name)
        return tty.hasPrefix("tty") ? "/dev/" + tty : nil
    }

    /// The executable path and environment a process started with (KERN_PROCARGS2: argc,
    /// the path, padding, the arguments, then the environment, all NUL-terminated).
    private static func launchInfo(of pid: pid_t) -> (path: String, environment: [String: String])? {
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var size = 0
        guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return nil }
        var buffer = [UInt8](repeating: 0, count: size)
        guard sysctl(&mib, 3, &buffer, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return nil }

        let argc = buffer.withUnsafeBytes { $0.load(as: Int32.self) }
        var offset = MemoryLayout<Int32>.size
        func next() -> String? {
            guard offset < size else { return nil }
            let end = buffer[offset..<size].firstIndex(of: 0) ?? size
            defer { offset = end + 1 }
            return String(decoding: buffer[offset..<end], as: UTF8.self)
        }
        guard let path = next() else { return nil }
        while offset < size, buffer[offset] == 0 { offset += 1 }
        for _ in 0..<max(argc, 0) { _ = next() }
        var environment: [String: String] = [:]
        while let entry = next(), !entry.isEmpty {
            guard let equals = entry.firstIndex(of: "=") else { continue }
            environment[String(entry[..<equals])] = String(entry[entry.index(after: equals)...])
        }
        return (path, environment)
    }

    /// Runs a tool to completion, quietly; its output if it succeeded.
    @discardableResult
    private static func run(_ path: String, _ arguments: [String]) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return nil }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return process.terminationStatus == 0 ? String(decoding: data, as: UTF8.self) : nil
    }
}

// MARK: - View

/// The chevron that folds the list: the one spot where a click means something. Everywhere
/// else a press drags the indicator, so this view opts out of moving the window, and handles the
/// press itself without ever taking focus.
final class FoldButton: NSView {
    var onPress: () -> Void = {}
    var folded = false {
        didSet { if folded != oldValue { needsDisplay = true } }
    }

    override var mouseDownCanMoveWindow: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {}       // claim the press; act on release

    override func mouseUp(with event: NSEvent) {
        if bounds.contains(convert(event.locationInWindow, from: nil)) { onPress() }
    }

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .button }
    override func accessibilityLabel() -> String? { folded ? "Unfold list" : "Fold list" }
    override func accessibilityPerformPress() -> Bool {
        onPress()
        return true
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(white: 1, alpha: 0.08).setFill()
        NSBezierPath(ovalIn: bounds.insetBy(dx: 1, dy: 1)).fill()

        // Up to unfold (the rows open upward), down to fold them away.
        let c = CGPoint(x: bounds.midX, y: bounds.midY)
        let rise: CGFloat = folded ? 2.5 : -2.5
        let chevron = NSBezierPath()
        chevron.move(to: CGPoint(x: c.x - 4.5, y: c.y - rise))
        chevron.line(to: CGPoint(x: c.x, y: c.y + rise))
        chevron.line(to: CGPoint(x: c.x + 4.5, y: c.y - rise))
        chevron.lineWidth = 1.6
        chevron.lineCapStyle = .round
        chevron.lineJoinStyle = .round
        NSColor(white: 1, alpha: 0.75).setStroke()
        chevron.stroke()
    }
}

/// Up to three sessions get big badges: hexagons with a transport-control glyph (play,
/// pause, check, dot), side by side, each captioned with its session's name and directory
/// once there is more than one. Four or more become a list, one row per session: a small
/// hexagon, the name over the directory, and the state in words. The list grows upward
/// from its bottom-right corner with the oldest session at the bottom, so rows already on
/// screen stay put when a new session starts.
final class TallyView: NSView {

    private struct Row {
        let session: SessionSnapshot
        let title: String          // session name, numbered when two share one
        let path: String           // directory, with ~ for home
    }

    /// One big badge, or one of a few side by side with a caption under each.
    private struct Chip {
        let width: CGFloat
        let height: CGFloat
        let radius: CGFloat
        let centerY: CGFloat
        let bounce: CGFloat
        let captioned: Bool
    }

    private static let single = Chip(width: 120, height: 120, radius: 44, centerY: 54, bounce: 18, captioned: false)
    private static let several = Chip(width: 124, height: 140, radius: 33, centerY: 88, bounce: 13, captioned: true)
    private static let listFrom = 4

    // The list.
    private static let maxRows = 16
    private static let padding: CGFloat = 8
    private static let rowHeight: CGFloat = 38
    private static let overflowHeight: CGFloat = 22
    private static let headerHeight: CGFloat = 26   // the count bar, which is all a folded list shows
    private static let headerGap: CGFloat = 6       // between the count bar and the rows
    private static let foldSize: CGFloat = 22
    private static let countFont = NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .semibold)
    private static let markRadius: CGFloat = 10
    private static let markX: CGFloat = 12         // mark centre, from the row's left edge
    private static let textX: CGFloat = 30         // text start, from the row's left edge
    private static let stateWidth: CGFloat = 46    // right-hand column for the state word
    private static let widthRange: ClosedRange<CGFloat> = 200...300
    private static let titleFont = NSFont.systemFont(ofSize: 12, weight: .semibold)
    private static let pathFont = NSFont.systemFont(ofSize: 10)
    private static let stateFont = NSFont.systemFont(ofSize: 10.5, weight: .medium)
    private static let captionTitleFont = NSFont.systemFont(ofSize: 10.5, weight: .semibold)
    private static let captionPathFont = NSFont.systemFont(ofSize: 9)

    private static let bounceDuration: CFTimeInterval = 1.5
    private static let washDuration: CFTimeInterval = 2.5

    /// Folded, the list shrinks to its count bar; the badges never fold.
    var folded = false {
        didSet {
            guard folded != oldValue else { return }
            positionFoldButton()
            needsDisplay = true
        }
    }
    var onFold: (Bool) -> Void = { _ in }
    /// A click on a row (or a badge) asks for that session's terminal.
    var onSelect: (SessionSnapshot) -> Void = { _ in }

    private let epoch = CACurrentMediaTime()
    private let foldButton = FoldButton()
    private var press: (at: NSPoint, dragging: Bool)?
    private var hovered: Int? {
        didSet {
            guard hovered != oldValue else { return }
            for index in [oldValue, hovered].compactMap({ $0 }) { setNeedsDisplay(itemRect(index)) }
        }
    }
    private var sessions: [SessionSnapshot] = []
    private var rows: [Row] = []
    private var counts: [(state: SessionState, count: Int)] = []
    private var overflow = 0
    private var listWidth = TallyView.widthRange.lowerBound
    private var barWidth = TallyView.widthRange.lowerBound
    private var enteredAt: [String: CFTimeInterval] = [:]
    private var primed = false

    var isList: Bool { sessions.count >= TallyView.listFrom }
    private var chip: Chip { sessions.count <= 1 ? TallyView.single : TallyView.several }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        foldButton.onPress = { [weak self] in
            guard let self = self else { return }
            self.onFold(!self.folded)
        }
        addSubview(foldButton)
        positionFoldButton()
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    var preferredSize: NSSize {
        guard isList else {
            return NSSize(width: CGFloat(max(1, rows.count)) * chip.width, height: chip.height)
        }
        let bar = 2 * TallyView.padding + TallyView.headerHeight
        if folded { return NSSize(width: barWidth, height: bar) }
        let height = bar + TallyView.headerGap + CGFloat(rows.count) * TallyView.rowHeight
            + (overflow > 0 ? TallyView.overflowHeight : 0)
        return NSSize(width: max(listWidth, barWidth), height: height)
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        positionFoldButton()
    }

    // MARK: Clicks and drags

    // A press that moves drags the indicator; one that doesn't is a click. Telling them apart
    // means the view handles the press itself rather than letting the window move.
    override var mouseDownCanMoveWindow: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        press = (event.locationInWindow, false)
    }

    /// The window follows the pointer; the pointer stays put in the window, so each step
    /// is measured from where the press began.
    override func mouseDragged(with event: NSEvent) {
        guard let start = press?.at, let window = window else { return }
        let dx = event.locationInWindow.x - start.x, dy = event.locationInWindow.y - start.y
        if press?.dragging == false && hypot(dx, dy) < 3 { return }   // a wobble, not a drag
        press?.dragging = true
        hovered = nil
        window.setFrameOrigin(NSPoint(x: window.frame.minX + dx, y: window.frame.minY + dy))
    }

    override func mouseUp(with event: NSEvent) {
        defer { press = nil }
        guard press?.dragging == false,
              let index = item(at: convert(event.locationInWindow, from: nil)) else { return }
        onSelect(rows[index].session)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        // activeAlways: this app is never the active one.
        addTrackingArea(NSTrackingArea(rect: .zero,
                                       options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }

    override func mouseMoved(with event: NSEvent) {
        hovered = item(at: convert(event.locationInWindow, from: nil))
    }

    override func mouseExited(with event: NSEvent) {
        hovered = nil
    }

    /// The row or badge under a point, if it stands for a session.
    private func item(at point: NSPoint) -> Int? {
        guard !rows.isEmpty, !(isList && folded) else { return nil }
        let index = (0..<rows.count).first { itemRect($0).contains(point) }
        return index
    }

    private func itemRect(_ index: Int) -> NSRect {
        isList
            ? rowRect(index)
            : NSRect(x: CGFloat(index) * chip.width, y: 0, width: chip.width, height: bounds.height)
    }

    /// At the right end of the count bar, which sits at the bottom so it stays put while
    /// the rows fold away above it.
    private func positionFoldButton() {
        foldButton.isHidden = !isList
        foldButton.folded = folded
        let size = TallyView.foldSize
        foldButton.frame = NSRect(x: bounds.maxX - TallyView.padding - size - 2,
                                  y: TallyView.padding + (TallyView.headerHeight - size) / 2,
                                  width: size, height: size)
    }

    func apply(_ next: [SessionSnapshot]) {
        defer { primed = true }
        guard next != sessions else { return }
        // On the first look nothing has just happened, so nothing replays its entry.
        let now = primed ? CACurrentMediaTime() : epoch - 60
        let previous = Dictionary(uniqueKeysWithValues: sessions.map { ($0.id, $0.state) })
        for session in next where previous[session.id] != session.state {
            enteredAt[session.id] = now          // this session just changed: replay its entry
        }
        let ids = Set(next.map { $0.id })
        enteredAt = enteredAt.filter { ids.contains($0.key) }
        sessions = next
        recomputeRows()
        positionFoldButton()
        needsDisplay = true
    }

    private func recomputeRows() {
        // Number repeated names in age order, over every session, so a row keeps its name
        // whether or not the cap hides its siblings.
        var seen: [String: Int] = [:]
        var titles: [String: String] = [:]
        for session in sessions {
            let base = session.title ?? (session.label.isEmpty ? "Claude" : session.label)
            let n = (seen[base] ?? 0) + 1
            seen[base] = n
            titles[session.id] = n == 1 ? base : "\(base) \(n)"
        }

        // Over the cap, the sessions that most want me survive; the rest become "+N more".
        var visible = sessions
        overflow = 0
        if sessions.count > TallyView.maxRows {
            let chosen = Set(sessions.enumerated()
                .sorted {
                    $0.element.state.urgency != $1.element.state.urgency
                        ? $0.element.state.urgency > $1.element.state.urgency
                        : $0.offset < $1.offset
                }
                .prefix(TallyView.maxRows - 1)
                .map { $0.offset })
            visible = sessions.enumerated().filter { chosen.contains($0.offset) }.map { $0.element }
            overflow = sessions.count - visible.count
        }
        rows = visible.map { session in
            Row(session: session,
                title: titles[session.id] ?? "",
                path: session.path.map { ($0 as NSString).abbreviatingWithTildeInPath } ?? "")
        }

        // Wide enough for the longest name or path, within reason; longer ones truncate.
        func measure(_ text: String, _ font: NSFont) -> CGFloat {
            ceil((text as NSString).size(withAttributes: [.font: font]).width)
        }
        let widest = rows.map {
            max(measure($0.title, TallyView.titleFont) + 8 + TallyView.stateWidth,
                measure($0.path, TallyView.pathFont))
        }.max() ?? 0
        let wanted = 2 * TallyView.padding + TallyView.textX + widest
        listWidth = min(max(wanted, TallyView.widthRange.lowerBound), TallyView.widthRange.upperBound)

        // The count bar covers every session, hidden or not, most urgent first.
        counts = SessionState.byUrgency.compactMap { state in
            let n = sessions.filter { $0.state == state }.count
            return n > 0 ? (state, n) : nil
        }
        let items = counts.map { TallyView.countItemWidth($0.count) }.reduce(0, +)
        barWidth = ceil(2 * TallyView.padding + 4 + items + TallyView.foldSize + 8)
    }

    /// A count in the bar: small mark, then the number, then a gap before the next.
    private static func countItemWidth(_ count: Int) -> CGFloat {
        2 * 8 + 5 + ceil(("\(count)" as NSString).size(withAttributes: [.font: countFont]).width) + 12
    }

    /// False once everything on screen has settled, so we can skip redraws.
    var isAnimating: Bool {
        if rows.isEmpty { return true }                     // the idle placeholder breathes
        if isList && folded {
            return sessions.contains { isMoving($0, settlesAfter: TallyView.washDuration) }
        }
        let settle = isList ? TallyView.washDuration : TallyView.bounceDuration
        return rows.contains { isMoving($0.session, settlesAfter: settle) }
    }

    /// Asks for a redraw of only what moves: text costs far more to draw than the marks,
    /// and it never moves. In the list that's the column of marks plus any row still
    /// washing; folded, the whole (small) bar; under the badges, everything above the
    /// captions.
    func invalidateMotion() {
        guard isList else {
            let top: CGFloat = chip.captioned ? 48 : 0
            setNeedsDisplay(NSRect(x: 0, y: top, width: bounds.width, height: bounds.height - top))
            return
        }
        if folded {
            // The bar's marks move and its numbers don't; all of it only while it washes.
            guard landedAge >= TallyView.washDuration else {
                setNeedsDisplay(bounds)
                return
            }
            var x = TallyView.padding + 4
            for item in counts {
                setNeedsDisplay(NSRect(x: x - 2, y: 0, width: 20, height: bounds.height))
                x += TallyView.countItemWidth(item.count)
            }
            return
        }
        setNeedsDisplay(NSRect(x: 0, y: rowsBottom, width: TallyView.padding + TallyView.textX - 2,
                               height: bounds.height - rowsBottom))
        for (index, row) in rows.enumerated() where isMoving(row.session, settlesAfter: TallyView.washDuration)
            && row.session.state == .done {
            setNeedsDisplay(rowRect(index).insetBy(dx: -4, dy: 0))
        }
    }

    private func isMoving(_ session: SessionSnapshot, settlesAfter duration: CFTimeInterval) -> Bool {
        switch session.state {
        case .working, .waiting, .idle: return true
        case .done: return CACurrentMediaTime() - (enteredAt[session.id] ?? 0) < duration
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        if isList {
            drawList()
        } else if rows.isEmpty {
            drawChip(Row(session: SessionSnapshot(id: "", state: .idle, label: ""), title: "", path: ""),
                     in: bounds, chip: TallyView.single)
        } else {
            let chip = self.chip
            for (index, row) in rows.enumerated() {
                drawChip(row, in: NSRect(x: CGFloat(index) * chip.width, y: 0,
                                         width: chip.width, height: bounds.height),
                         chip: chip, highlighted: index == hovered)
            }
        }
    }

    // MARK: Badges

    private func drawChip(_ row: Row, in rect: NSRect, chip: Chip, highlighted: Bool = false) {
        if chip.captioned && needsToDraw(NSRect(x: rect.minX, y: 0, width: rect.width, height: 48)) {
            drawCaption(row, in: rect, highlighted: highlighted)
        }

        let session = row.session
        let age = CACurrentMediaTime() - (enteredAt[session.id] ?? epoch - 60)
        let move = motion(session.state, age: age, working: 5, waiting: 3, bounce: chip.bounce)
        let glow = move.glow

        let centre = CGPoint(x: rect.midX, y: chip.centerY + move.bob)
        let radius = chip.radius

        // Smoked-glass chip, so the mark survives a light wallpaper.
        let plate = Hexagon.path(center: centre, radius: radius)
        NSColor(white: highlighted && !chip.captioned ? 0.2 : 0.04, alpha: 0.44).setFill()
        plate.fill()
        NSColor(white: 1, alpha: 0.10).setStroke()
        plate.lineWidth = 1
        plate.stroke()

        guard let ctx = NSGraphicsContext.current else { return }
        ctx.saveGraphicsState()
        ctx.cgContext.setAlpha(glow)

        // Segmented ring: six edges, corners left open.
        let accent = session.state.accent
        let pts = Hexagon.points(center: centre, radius: radius * 0.82)
        let ring = NSBezierPath()
        ring.lineWidth = 2.5 * radius / 44
        ring.lineCapStyle = .round
        for i in 0..<6 {
            let a = pts[i], b = pts[(i + 1) % 6]
            ring.move(to: lerp(a, b, 0.17))
            ring.line(to: lerp(a, b, 0.83))
        }
        accent.withAlphaComponent(0.9).setStroke()
        ring.stroke()

        accent.setFill()
        accent.setStroke()
        drawGlyph(session.state, at: centre, scale: radius / 44)
        ctx.restoreGraphicsState()
    }

    /// Which session this badge is: its name over its directory, on a card of their own so
    /// they stay legible on any wallpaper.
    private func drawCaption(_ row: Row, in rect: NSRect, highlighted: Bool) {
        let card = NSRect(x: rect.minX + 6, y: 8, width: rect.width - 12, height: 38)
        NSColor(white: highlighted ? 0.2 : 0.04, alpha: 0.55).setFill()
        NSBezierPath(roundedRect: card, xRadius: 8, yRadius: 8).fill()

        let inner = card.insetBy(dx: 7, dy: 3)
        drawText(row.title,
                 in: NSRect(x: inner.minX, y: inner.midY, width: inner.width, height: inner.height / 2),
                 font: TallyView.captionTitleFont, color: NSColor(white: 0.95, alpha: 1), alignment: .center)
        drawText(row.path,
                 in: NSRect(x: inner.minX, y: inner.minY, width: inner.width, height: inner.height / 2),
                 font: TallyView.captionPathFont, color: NSColor(white: 0.95, alpha: 0.6),
                 alignment: .center, truncation: .byTruncatingMiddle)
    }

    // MARK: List

    private func drawList() {
        // One smoked-glass plate behind the list, so it survives a light wallpaper.
        let plate = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 12, yRadius: 12)
        NSColor(white: 0.04, alpha: 0.55).setFill()
        plate.fill()
        NSColor(white: 1, alpha: 0.10).setStroke()
        plate.lineWidth = 1
        plate.stroke()

        let bar = NSRect(x: TallyView.padding, y: TallyView.padding,
                         width: bounds.width - 2 * TallyView.padding, height: TallyView.headerHeight)
        if needsToDraw(bar.insetBy(dx: 0, dy: -6)) { drawCountBar(in: bar) }
        guard !folded else { return }

        // A hairline between the count bar and the rows.
        NSColor(white: 1, alpha: 0.08).setFill()
        NSRect(x: bar.minX + 4, y: rowsBottom - TallyView.headerGap / 2 - 0.5, width: bar.width - 8, height: 1).fill()

        for (index, row) in rows.enumerated() {
            let rect = rowRect(index)
            // Bounces reach a little beyond the row.
            if needsToDraw(rect.insetBy(dx: -4, dy: -6)) { draw(row, in: rect, highlighted: index == hovered) }
        }
        if overflow > 0 {
            let y = rowsBottom + CGFloat(rows.count) * TallyView.rowHeight
            drawText("+\(overflow) more",
                     in: NSRect(x: TallyView.padding + TallyView.textX, y: y,
                                width: bounds.width - 2 * TallyView.padding - TallyView.textX,
                                height: TallyView.overflowHeight),
                     font: TallyView.titleFont, color: SessionState.idle.accent, alignment: .left)
        }
    }

    /// The rows start above the count bar.
    private var rowsBottom: CGFloat {
        TallyView.padding + TallyView.headerHeight + TallyView.headerGap
    }

    private func rowRect(_ index: Int) -> NSRect {
        NSRect(x: TallyView.padding, y: rowsBottom + CGFloat(index) * TallyView.rowHeight,
               width: bounds.width - 2 * TallyView.padding, height: TallyView.rowHeight)
    }

    /// How long ago the most recent turn landed.
    private var landedAge: CFTimeInterval {
        let now = CACurrentMediaTime()
        return sessions.filter { $0.state == .done }
            .map { now - (enteredAt[$0.id] ?? epoch - 60) }
            .min() ?? .infinity
    }

    /// Sessions counted by state, most urgent first. Folded, the bar is all there is, so it
    /// carries the motion the rows otherwise would, the green wash and bounce included;
    /// unfolded, the rows move and the bar holds still.
    private func drawCountBar(in bar: NSRect) {
        let landed = landedAge
        if folded && landed < TallyView.washDuration {
            let fade = CGFloat(1 - landed / TallyView.washDuration)
            SessionState.done.accent.withAlphaComponent(0.30 * fade).setFill()
            NSBezierPath(roundedRect: bar.insetBy(dx: -3, dy: 0), xRadius: 8, yRadius: 8).fill()
        }

        var x = bar.minX + 4
        for (state, count) in counts {
            let move = folded ? motion(state, age: landed) : (bob: 0, glow: 1)
            drawSmallMark(state, at: CGPoint(x: x + 8, y: bar.midY + move.bob), glow: move.glow, radius: 8)
            let number = NSRect(x: x + 21, y: bar.minY, width: 40, height: bar.height)
            if needsToDraw(number) {
                drawText("\(count)", in: number, font: TallyView.countFont, color: state.accent, alignment: .left)
            }
            x += TallyView.countItemWidth(count)
        }
    }

    /// How a mark moves: a slow bob while working or waiting, one damped bounce when a
    /// turn lands and then dead still, a breath while idle. Heights are in points; the
    /// defaults suit the list's small marks.
    private func motion(_ state: SessionState, age: CFTimeInterval, working: CGFloat = 2,
                        waiting: CGFloat = 1.5, bounce: CGFloat = 5) -> (bob: CGFloat, glow: CGFloat) {
        let clock = CACurrentMediaTime() - epoch
        switch state {
        case .working:
            return (working * CGFloat(sin(2 * .pi * clock / 1.6)), 1)
        case .waiting:
            return (waiting * CGFloat(sin(2 * .pi * clock / 1.1)), 1)
        case .done:
            guard age < TallyView.bounceDuration else { return (0, 1) }
            return (bounce * CGFloat(exp(-3.2 * age) * abs(sin(2 * .pi * age / 0.7))), 1)
        case .idle:
            return (0, breath())
        }
    }

    private func draw(_ row: Row, in rect: NSRect, highlighted: Bool) {
        let state = row.session.state
        let age = CACurrentMediaTime() - (enteredAt[row.session.id] ?? epoch - 60)

        // Under the pointer: clicking brings this session's terminal forward.
        if highlighted {
            NSColor(white: 1, alpha: 0.08).setFill()
            NSBezierPath(roundedRect: rect.insetBy(dx: -3, dy: 1), xRadius: 7, yRadius: 7).fill()
        }

        // The one memorable moment: a finished turn washes its row green and its mark
        // bounces once, then both settle and stay.
        if state == .done && age < TallyView.washDuration {
            let fade = CGFloat(1 - age / TallyView.washDuration)
            state.accent.withAlphaComponent(0.30 * fade).setFill()
            NSBezierPath(roundedRect: rect.insetBy(dx: -3, dy: 1), xRadius: 7, yRadius: 7).fill()
        }
        let move = motion(state, age: age)
        drawSmallMark(state, at: CGPoint(x: rect.minX + TallyView.markX, y: rect.midY + move.bob), glow: move.glow)

        let textLeft = rect.minX + TallyView.textX
        guard needsToDraw(NSRect(x: textLeft, y: rect.minY, width: rect.maxX - textLeft, height: rect.height))
        else { return }

        // Name and state on the upper line, the directory under them; with no directory
        // known yet, the name line sits centred instead.
        let upper = row.path.isEmpty
            ? NSRect(x: textLeft, y: rect.minY, width: rect.maxX - textLeft, height: rect.height)
            : NSRect(x: textLeft, y: rect.midY, width: rect.maxX - textLeft, height: rect.height / 2 - 2)
        let lower = NSRect(x: textLeft, y: rect.minY + 2, width: rect.maxX - textLeft, height: rect.height / 2 - 2)
        drawText(row.title,
                 in: NSRect(x: upper.minX, y: upper.minY, width: upper.width - TallyView.stateWidth - 6,
                            height: upper.height),
                 font: TallyView.titleFont, color: NSColor(white: 0.95, alpha: 1), alignment: .left)
        drawText(state.rawValue,
                 in: NSRect(x: upper.maxX - TallyView.stateWidth, y: upper.minY, width: TallyView.stateWidth,
                            height: upper.height),
                 font: TallyView.stateFont, color: state.accent, alignment: .right)
        drawText(row.path, in: lower, font: TallyView.pathFont, color: NSColor(white: 0.95, alpha: 0.6),
                 alignment: .left, truncation: .byTruncatingMiddle)
    }

    /// A small tinted hexagon with the state's glyph: a solid outline rather than the big
    /// badge's segmented ring, which turns to noise at this size.
    private func drawSmallMark(_ state: SessionState, at c: CGPoint, glow: CGFloat,
                               radius: CGFloat = TallyView.markRadius) {
        guard let ctx = NSGraphicsContext.current else { return }
        ctx.saveGraphicsState()
        ctx.cgContext.setAlpha(glow)

        let accent = state.accent
        let plate = Hexagon.path(center: c, radius: radius)
        accent.withAlphaComponent(0.16).setFill()
        plate.fill()
        accent.withAlphaComponent(0.9).setStroke()
        plate.lineWidth = 1.2
        plate.stroke()

        accent.setFill()
        accent.setStroke()
        drawGlyph(state, at: c, scale: radius / 44 * 1.3)
        ctx.restoreGraphicsState()
    }

    // MARK: Shared drawing

    /// Slow breath for idle marks; the shape itself holds steady.
    private func breath() -> CGFloat {
        0.32 + 0.58 * CGFloat(0.5 + 0.5 * sin(2 * .pi * (CACurrentMediaTime() - epoch) / 4.0))
    }

    private func drawGlyph(_ state: SessionState, at c: CGPoint, scale s: CGFloat) {
        switch state {
        case .working:
            // Play: running.
            let p = NSBezierPath()
            p.move(to: CGPoint(x: c.x - 9 * s, y: c.y + 13 * s))
            p.line(to: CGPoint(x: c.x + 14 * s, y: c.y))
            p.line(to: CGPoint(x: c.x - 9 * s, y: c.y - 13 * s))
            p.close()
            p.lineJoinStyle = .round
            p.lineWidth = 4 * s
            p.fill()
            p.stroke()

        case .waiting:
            // Pause: stopped, waiting on me.
            for dx in [CGFloat(-8), CGFloat(3)] {
                NSBezierPath(roundedRect: NSRect(x: c.x + dx * s, y: c.y - 13 * s,
                                                 width: 5.5 * s, height: 26 * s),
                             xRadius: 2.5 * s, yRadius: 2.5 * s).fill()
            }

        case .done:
            // Check: finished.
            let p = NSBezierPath()
            p.move(to: CGPoint(x: c.x - 13 * s, y: c.y + 1 * s))
            p.line(to: CGPoint(x: c.x - 4 * s, y: c.y - 9 * s))
            p.line(to: CGPoint(x: c.x + 14 * s, y: c.y + 12 * s))
            p.lineWidth = 6 * s
            p.lineCapStyle = .round
            p.lineJoinStyle = .round
            p.stroke()

        case .idle:
            // A single dot: nothing running.
            NSBezierPath(ovalIn: NSRect(x: c.x - 5.5 * s, y: c.y - 5.5 * s,
                                        width: 11 * s, height: 11 * s)).fill()
        }
    }

    /// One line, vertically centred in its rect, truncated where it doesn't fit.
    private func drawText(_ text: String, in rect: NSRect, font: NSFont, color: NSColor,
                          alignment: NSTextAlignment, truncation: NSLineBreakMode = .byTruncatingTail) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = alignment
        paragraph.lineBreakMode = truncation
        let string = NSAttributedString(string: text, attributes: [
            .font: font,
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ])
        let height = ceil(font.ascender - font.descender)
        string.draw(in: NSRect(x: rect.minX, y: rect.midY - height / 2, width: rect.width, height: height))
    }

    private func lerp(_ a: CGPoint, _ b: CGPoint, _ t: CGFloat) -> CGPoint {
        CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t)
    }
}

// MARK: - Window

/// An accessory panel must never take key or main status away from the terminal.
final class TallyPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

// MARK: - Settings window

/// An accessory app has no main menu to catch keys, so the window handles its own, and
/// swallows the rest: an unhandled key would make the system beep.
final class SettingsWindow: NSWindow {
    override func keyDown(with event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask).subtracting(.capsLock)
        let escape = event.keyCode == 53
        let commandW = flags == .command && event.charactersIgnoringModifiers?.lowercased() == "w"
        if escape || commandW { performClose(nil) }
    }
}

/// CTally's one ordinary window: an on/off switch and what it is showing. Opens from the
/// menu bar icon, the right-click menu, or by opening the app again.
final class SettingsController: NSObject, NSWindowDelegate {
    private static let width: CGFloat = 400
    static let opacityRange = 0.3...1.0

    var onToggle: (Bool) -> Void = { _ in }
    var onOpacity: (Double) -> Void = { _ in }

    private let window = SettingsWindow(contentRect: .zero, styleMask: [.titled, .closable],
                                        backing: .buffered, defer: true)
    private let icon = NSImageView()
    private let status = NSTextField(labelWithString: "")
    private let toggle = NSSwitch()
    private let opacity = NSSlider(value: 1, minValue: SettingsController.opacityRange.lowerBound,
                                   maxValue: SettingsController.opacityRange.upperBound,
                                   target: nil, action: nil)
    private let opacityValue = NSTextField(labelWithString: "")

    override init() {
        super.init()
        window.title = "CTally"
        window.isReleasedWhenClosed = false
        window.isRestorable = false
        // Open on the Space I'm looking at, even beside a full-screen terminal.
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.delegate = self
        let content = makeContent()
        window.contentView = content
        window.setContentSize(content.fittingSize)
        window.center()
    }

    func show() {
        if NSApp.isHidden { NSApp.unhide(nil) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    /// Called on every poll, so it only touches what changed; setting the switch mid-click
    /// would fight the click.
    func update(enabled: Bool, sessions: [SessionSnapshot]) {
        let state: NSControl.StateValue = enabled ? .on : .off
        if toggle.state != state { toggle.state = state }
        icon.alphaValue = enabled ? 1 : 0.35
        let text = enabled ? SettingsController.describe(sessions) : "Off · hidden"
        if status.stringValue != text { status.stringValue = text }
    }

    func update(opacity value: Double) {
        opacity.doubleValue = value
        opacityValue.stringValue = "\(Int((value * 100).rounded()))%"
    }

    func windowWillClose(_ notification: Notification) {
        // Hand the keyboard back to whatever I was using; the indicator ignores hiding.
        NSApp.hide(nil)
    }

    @objc private func toggled(_ sender: NSSwitch) {
        onToggle(sender.state == .on)
    }

    /// Continuous, so the indicator fades live under the slider.
    @objc private func opacityChanged(_ sender: NSSlider) {
        update(opacity: sender.doubleValue)
        onOpacity(sender.doubleValue)
    }

    /// What the indicator is showing, in words: "On · 1 working, 2 done".
    private static func describe(_ sessions: [SessionSnapshot]) -> String {
        let counts = SessionState.byUrgency.compactMap { state -> String? in
            let n = sessions.filter { $0.state == state }.count
            return n > 0 ? "\(n) \(state.rawValue)" : nil
        }
        return counts.isEmpty ? "On · no Claude Code sessions" : "On · " + counts.joined(separator: ", ")
    }

    private func makeContent() -> NSView {
        icon.image = NSApp.applicationIconImage
        icon.imageScaling = .scaleProportionallyUpOrDown

        let title = NSTextField(labelWithString: "Show CTally")
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        status.font = .systemFont(ofSize: 11)
        status.textColor = .secondaryLabelColor
        status.lineBreakMode = .byTruncatingTail

        let labels = NSStackView(views: [title, status])
        labels.orientation = .vertical
        labels.distribution = .fill      // exact spacing and edges, so the pair can't float
        labels.alignment = .leading
        labels.spacing = 2

        toggle.target = self
        toggle.action = #selector(toggled(_:))
        toggle.setAccessibilityLabel("Show CTally")

        let opacityTitle = NSTextField(labelWithString: "Opacity")
        opacity.target = self
        opacity.action = #selector(opacityChanged(_:))
        opacity.isContinuous = true
        opacity.setAccessibilityLabel("Opacity")
        opacityValue.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        opacityValue.textColor = .secondaryLabelColor
        opacityValue.alignment = .right

        let hint = NSTextField(wrappingLabelWithString:
            "Off stays off, even after a restart. The hexagon icon in the menu bar switches it too.")
        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor

        let quit = NSButton(title: "Quit", target: NSApp, action: #selector(NSApplication.terminate(_:)))

        let rule = NSBox()
        rule.boxType = .separator

        // The text and slider take the slack, pinning the switch, percentage and Quit to the
        // right edge. The opacity line is indented to sit under "Show CTally".
        let row = NSStackView(views: [icon, labels, toggle])
        let fade = NSStackView(views: [opacityTitle, opacity, opacityValue])
        let footer = NSStackView(views: [hint, quit])
        for line in [row, fade, footer] {
            line.distribution = .fill
            line.alignment = .centerY
            line.spacing = 12
        }
        fade.edgeInsets = NSEdgeInsets(top: 0, left: 52, bottom: 0, right: 0)
        labels.setHuggingPriority(.defaultLow, for: .horizontal)
        hint.setContentHuggingPriority(.defaultLow, for: .horizontal)
        opacity.setContentHuggingPriority(.defaultLow, for: .horizontal)
        for fixed in [toggle, quit, opacityTitle] as [NSView] {
            fixed.setContentHuggingPriority(.required, for: .horizontal)
        }
        hint.preferredMaxLayoutWidth = SettingsController.width - 40 - 12 - quit.fittingSize.width

        let stack = NSStackView(views: [row, fade, rule, footer])
        stack.orientation = .vertical
        stack.spacing = 14
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)

        NSLayoutConstraint.activate([
            stack.widthAnchor.constraint(equalToConstant: SettingsController.width),
            icon.widthAnchor.constraint(equalToConstant: 40),
            icon.heightAnchor.constraint(equalToConstant: 40),
            opacityValue.widthAnchor.constraint(equalToConstant: 38),
        ] + [row, fade, rule, footer].map {
            $0.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -40)
        })
        return stack
    }
}

// MARK: - Controller

final class AppController: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate {
    private static let inset: CGFloat = 24
    private static let anchorKey = "WindowAnchor"
    private static let enabledKey = "Enabled"
    private static let opacityKey = "Opacity"
    private static let foldedKey = "ListFolded"

    private let focuser = SessionFocuser()
    private let reader = StateReader(
        directory: URL(fileURLWithPath: (NSHomeDirectory() as NSString).appendingPathComponent(".claude/ctally.d")),
        transcripts: URL(fileURLWithPath: (NSHomeDirectory() as NSString).appendingPathComponent(".claude/projects")))
    private var panel: TallyPanel!
    private var view: TallyView!
    private var statusItem: NSStatusItem!
    private var showItem: NSMenuItem!
    private var foldItem: NSMenuItem!
    private var settings: SettingsController?
    private var sessions: [SessionSnapshot] = []
    private var timers: [Timer] = []
    private var restoring = false

    /// Off is remembered, so once switched off it stays off through restarts and logins.
    private var isEnabled: Bool {
        UserDefaults.standard.bool(forKey: AppController.enabledKey)
    }

    /// See-through by default, so the list doesn't blot out what's behind it.
    private var opacity: Double {
        let range = SettingsController.opacityRange
        return min(max(UserDefaults.standard.double(forKey: AppController.opacityKey),
                       range.lowerBound), range.upperBound)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: [AppController.enabledKey: true,
                                                  AppController.opacityKey: 0.8])

        panel = TallyPanel(contentRect: NSRect(x: 0, y: 0, width: 120, height: 120),
                         styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered,
                         defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        panel.isMovableByWindowBackground = false   // the view drags it, to tell drags from clicks
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        // Closing the settings window hides the app to hand focus back; the indicator stays put.
        panel.canHide = false
        panel.alphaValue = CGFloat(opacity)
        panel.delegate = self

        view = TallyView(frame: NSRect(x: 0, y: 0, width: 120, height: 120))
        view.menu = makeMenu()
        view.folded = UserDefaults.standard.bool(forKey: AppController.foldedKey)
        view.onFold = { [weak self] in self?.setFolded($0) }
        view.onSelect = { [weak self] in self?.focuser.focus($0.pid) }
        panel.contentView = view

        statusItem = makeStatusItem()
        refreshControls()

        if isEnabled {
            showTally()
        } else if !launchedAsLoginItem {
            // Opened by hand while off: show the way back on rather than nothing at all.
            showSettings()
        }
    }

    /// Opening the app while it runs (Spotlight, Finder, Launchpad) brings up the settings.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showSettings()
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopTimers()
    }

    func windowDidMove(_ notification: Notification) {
        guard !restoring else { return }
        saveAnchor()
    }

    private func setEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: AppController.enabledKey)
        if enabled { showTally() } else { hideTally() }
        refreshControls()
    }

    private func setOpacity(_ value: Double) {
        UserDefaults.standard.set(value, forKey: AppController.opacityKey)
        panel.alphaValue = CGFloat(opacity)
    }

    private func showTally() {
        poll()
        // Not makeKeyAndOrderFront: an accessory app has no business holding key status.
        panel.orderFrontRegardless()
        startTimers()
    }

    /// Off means dormant: nothing on screen and no timers waking the CPU.
    private func hideTally() {
        stopTimers()
        panel.orderOut(nil)
    }

    private func poll() {
        sessions = reader.poll()
        view.apply(sessions)
        fit()
        settings?.update(enabled: isEnabled, sessions: sessions)
    }

    private func showSettings() {
        if settings == nil {
            let controller = SettingsController()
            controller.onToggle = { [weak self] in self?.setEnabled($0) }
            controller.onOpacity = { [weak self] in self?.setOpacity($0) }
            settings = controller
        }
        refreshControls()
        settings?.update(opacity: opacity)
        settings?.show()
    }

    /// The menu bar icon, its menu and the settings window all mirror the one setting.
    private func refreshControls() {
        let enabled = isEnabled
        showItem.state = enabled ? .on : .off
        statusItem.button?.appearsDisabled = !enabled
        settings?.update(enabled: enabled, sessions: sessions)
    }

    /// The indicator's right-click menu.
    private func makeMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false
        let hide = menu.addItem(withTitle: "Hide CTally", action: #selector(hideFromMenu), keyEquivalent: "")
        hide.target = self
        hide.toolTip = "Bring it back from the hexagon icon in the menu bar."
        foldItem = menu.addItem(withTitle: "Fold List", action: #selector(foldFromMenu), keyEquivalent: "")
        foldItem.target = self
        menu.addItem(withTitle: "Settings…", action: #selector(settingsFromMenu), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "").target = NSApp
        menu.delegate = self
        return menu
    }

    /// Folding only means something while the list is showing.
    func menuNeedsUpdate(_ menu: NSMenu) {
        foldItem.isHidden = !view.isList
        foldItem.title = view.folded ? "Unfold List" : "Fold List"
    }

    /// Remembered, so the list comes back the way I left it.
    private func setFolded(_ folded: Bool) {
        UserDefaults.standard.set(folded, forKey: AppController.foldedKey)
        view.folded = folded
        fit()
    }

    @objc private func foldFromMenu() {
        setFolded(!view.folded)
    }

    /// The menu bar icon stays put while the indicator is off, dimmed, so it is always one
    /// click from coming back.
    private func makeStatusItem() -> NSStatusItem {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = AppController.menuBarIcon()
        item.button?.toolTip = "CTally"

        let menu = NSMenu()
        menu.autoenablesItems = false
        showItem = menu.addItem(withTitle: "Show CTally", action: #selector(toggleFromMenuBar), keyEquivalent: "")
        showItem.target = self
        menu.addItem(withTitle: "Settings…", action: #selector(settingsFromMenu), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit CTally", action: #selector(NSApplication.terminate(_:)),
                     keyEquivalent: "").target = NSApp
        item.menu = menu
        return item
    }

    /// The hexagon and play mark as a template image, so the menu bar tints it for
    /// light and dark.
    private static func menuBarIcon() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            let c = CGPoint(x: rect.midX, y: rect.midY)
            let hexagon = Hexagon.path(center: c, radius: 7.5)
            hexagon.lineWidth = 1.5
            hexagon.lineJoinStyle = .round

            let play = NSBezierPath()
            play.move(to: CGPoint(x: c.x - 2, y: c.y + 3.25))
            play.line(to: CGPoint(x: c.x + 3.5, y: c.y))
            play.line(to: CGPoint(x: c.x - 2, y: c.y - 3.25))
            play.close()

            NSColor.black.set()
            hexagon.stroke()
            play.fill()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "CTally"
        return image
    }

    @objc private func toggleFromMenuBar() {
        setEnabled(!isEnabled)
    }

    @objc private func hideFromMenu() {
        setEnabled(false)
    }

    @objc private func settingsFromMenu() {
        showSettings()
    }

    /// Login launches carry a marker in their open-application event, readable only while
    /// applicationDidFinishLaunching runs.
    private var launchedAsLoginItem: Bool {
        guard let event = NSAppleEventManager.shared().currentAppleEvent else { return false }
        return event.eventID == kAEOpenApplication
            && event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }

    /// Size to what the view wants, keeping the bottom-right corner planted. Also does the
    /// initial placement, so it must compare the origin and not just the size.
    private func fit() {
        let wanted = view.preferredSize
        let anchor = restoredAnchor()
        var target = NSRect(x: anchor.x - wanted.width, y: anchor.y,
                            width: wanted.width, height: wanted.height)
        // A tall list slides down rather than run off the top of the screen; the saved
        // corner stays where I put it, for when the list shrinks again.
        if let screen = NSScreen.screens.first(where: { $0.frame.contains(CGPoint(x: anchor.x - 1, y: anchor.y + 1)) }) {
            let visible = screen.visibleFrame
            target.origin.y = max(min(target.minY, visible.maxY - target.height), visible.minY)
        }
        let frame = panel.frame
        guard abs(target.minX - frame.minX) > 0.5 || abs(target.minY - frame.minY) > 0.5
           || abs(target.width - frame.width) > 0.5 || abs(target.height - frame.height) > 0.5
        else { return }
        restoring = true                 // a frame I set is not the user repositioning it
        panel.setFrame(target, display: true)
        restoring = false
        view.needsDisplay = true
    }

    private func startTimers() {
        guard timers.isEmpty else { return }
        schedule(every: 1.0 / 24.0) { [weak self] in
            guard let self = self, self.view.isAnimating else { return }
            self.view.invalidateMotion()
        }
        schedule(every: 1.0 / 5.0) { [weak self] in
            self?.poll()
        }
    }

    private func stopTimers() {
        timers.forEach { $0.invalidate() }
        timers.removeAll()
    }

    /// Timers must live in .common mode or they freeze for the duration of a drag.
    private func schedule(every interval: TimeInterval, _ body: @escaping () -> Void) {
        let timer = Timer(timeInterval: interval, repeats: true) { _ in body() }
        RunLoop.main.add(timer, forMode: .common)
        timers.append(timer)
    }

    private func saveAnchor() {
        let frame = panel.frame
        UserDefaults.standard.set([Double(frame.maxX), Double(frame.minY)],
                                  forKey: AppController.anchorKey)
    }

    /// The bottom-right corner is what's remembered, so it grows leftward and upward.
    private func restoredAnchor() -> CGPoint {
        if let saved = UserDefaults.standard.array(forKey: AppController.anchorKey) as? [Double],
           saved.count == 2 {
            let anchor = CGPoint(x: saved[0], y: saved[1])
            if isOnScreen(anchor) { return anchor }
        }
        return defaultAnchor()
    }

    /// A position saved on a monitor that is no longer attached must not come back off-screen.
    private func isOnScreen(_ anchor: CGPoint) -> Bool {
        NSScreen.screens.contains {
            $0.visibleFrame.contains(CGPoint(x: anchor.x - 60, y: anchor.y + 60))
        }
    }

    private func defaultAnchor() -> CGPoint {
        guard let frame = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame else {
            return CGPoint(x: 120, y: 0)
        }
        let inset = AppController.inset
        return CGPoint(x: frame.maxX - inset, y: frame.minY + inset)
    }
}

// MARK: - Entry point
// Single-file swiftc builds allow top-level code, so no @main is needed.

let app = NSApplication.shared
let controller = AppController()
app.delegate = controller
// Must be set before run(), or the app claims a Dock icon.
app.setActivationPolicy(.accessory)
app.run()
