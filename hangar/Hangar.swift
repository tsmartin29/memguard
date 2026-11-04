import AppKit
import SwiftUI
import ServiceManagement
import AVFoundation
// Hangar: a menu-bar remote for memguard's own commands, with an F-22 button for afterburner. It runs these commands and shows their
// result line, nothing else:
//   afterburner on 2h / off  ·  tidy --only clean  ·  tidy --only save  ·  tidy --only backup  ·  tidy (all three)
// Jettison (tidy), Black Box (backup) and Alpha Strike (all three) first run `tidy --dry-run ...`, show that plan and wait for one Confirm. No network of its own, no admin
// rights. "Open at login" is OFF until you switch it on in the panel (SMAppService). Build: ./build.sh  Screenshots: HANGAR_SHOT=<dir>.
// The commands default to ~/.local/bin/tidy and ~/.local/bin/afterburner (install.sh links them there); HANGAR_TIDY and
// HANGAR_AFTERBURNER override the paths. The Lanes tab reads the memguard directory from HANGAR_MEMGUARD_DIR, else MEMGUARD_HOME,
// else ~/.config/memguard.

let HOMEDIR = NSHomeDirectory()
let ENV = ProcessInfo.processInfo.environment
let TIDY = ENV["HANGAR_TIDY"] ?? (HOMEDIR + "/.local/bin/tidy")
let AFTERBURNER = ENV["HANGAR_AFTERBURNER"] ?? (HOMEDIR + "/.local/bin/afterburner")
let SCRAMBLE = ENV["HANGAR_SCRAMBLE"] ?? (HOMEDIR + "/.local/bin/scramble")   // keep every agent slot filled

func runTool(_ path: String, _ args: [String]) -> (Int32, String) {
  let p = Process()
  p.executableURL = URL(fileURLWithPath: path); p.arguments = args
  var env = ENV; env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\(HOMEDIR)/.local/bin"
  p.environment = env
  let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
  do { try p.run() } catch { return (127, "could not start \(path): \(error.localizedDescription)") }
  let data = pipe.fileHandleForReading.readDataToEndOfFile()
  p.waitUntilExit()
  return (p.terminationStatus, String(decoding: data, as: UTF8.self))
}
func summaryLine(_ out: String) -> String {
  let lines = out.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
  if let s = lines.last(where: { $0.hasPrefix("SUMMARY:") }) { return String(s.dropFirst(8)).trimmingCharacters(in: .whitespaces) }
  return lines.last ?? "done"
}

// ---------------------------------------------------------------- lanes tab
// Read-only view of memguard's queue: running slots (heavy.lock, heavy.slotN, light.lock, light.slot2: pid + label) and waiting tickets
// (queue, queue_hold, queue_light, queue_light_hold: one file per job holding its label; "0-" prefix = priority), plus the last log lines.
// The memguard directory: HANGAR_MEMGUARD_DIR, else MEMGUARD_HOME (what memguard itself uses), else ~/.config/memguard.
let MGDIR = ENV["HANGAR_MEMGUARD_DIR"] ?? ENV["MEMGUARD_HOME"] ?? (HOMEDIR + "/.config/memguard")
struct LaneJob: Identifiable { let id: String; let label: String; let secs: Int; let alive: Bool; let priority: Bool; let held: Bool; var ticket: String = "" }
struct Lanes { var heavyRun = [LaneJob](), heavyQ = [LaneJob](), lightRun = [LaneJob](), lightQ = [LaneJob](), recent = [String](), paused = 0 }
func readLanes() -> Lanes {
  let fm = FileManager.default, now = Date()
  func text(_ p: String) -> String { ((try? String(contentsOfFile: p, encoding: .utf8)) ?? "").trimmingCharacters(in: .whitespacesAndNewlines) }
  func age(_ p: String) -> Int {
    guard let d = (try? fm.attributesOfItem(atPath: p))?[.modificationDate] as? Date else { return 0 }
    return max(0, Int(now.timeIntervalSince(d)))
  }
  let names = ((try? fm.contentsOfDirectory(atPath: MGDIR)) ?? []).sorted()
  func slots(_ pfx: [String]) -> [LaneJob] {
    names.filter { n in pfx.contains { n.hasPrefix($0) } }.compactMap { n in
      let d = MGDIR + "/" + n; var isDir: ObjCBool = false
      guard fm.fileExists(atPath: d, isDirectory: &isDir), isDir.boolValue else { return nil }
      let pid = Int32(text(d + "/pid")) ?? 0
      let lab = text(d + "/label")
      return LaneJob(id: n, label: lab.isEmpty ? "(no label)" : lab, secs: age(d), alive: pid > 1 && kill(pid, 0) == 0, priority: false, held: false)
    }
  }
  var manual = [String: Int]()
  for (i, line) in readManualOrder().enumerated() { if manual[line] == nil { manual[line] = i } }
  func queue(_ dirs: [String]) -> [LaneJob] {
    dirs.flatMap { q -> [LaneJob] in
      let d = MGDIR + "/" + q
      return ((try? fm.contentsOfDirectory(atPath: d)) ?? []).map { f in
        LaneJob(id: q + "/" + f, label: text(d + "/" + f), secs: age(d + "/" + f), alive: true, priority: f.hasPrefix("0-"), held: q.hasSuffix("_hold"), ticket: f)
      }
    }.sorted { a, b in
      let ka = (a.priority ? 0 : 1, manual[a.ticket] ?? Int.max, a.ticket), kb = (b.priority ? 0 : 1, manual[b.ticket] ?? Int.max, b.ticket)
      return ka < kb
    }
  }
  var l = Lanes()
  l.heavyRun = slots(["heavy.lock", "heavy.slot"]); l.lightRun = slots(["light.lock", "light.slot"])
  l.heavyQ = queue(["queue", "queue_hold"]); l.lightQ = queue(["queue_light", "queue_light_hold"])
  if let d = try? Data(contentsOf: URL(fileURLWithPath: MGDIR + "/paused.json")), let a = (try? JSONSerialization.jsonObject(with: d)) as? [Any] { l.paused = a.count }
  if let h = FileHandle(forReadingAtPath: MGDIR + "/heavy_queue.log") {
    let size = (try? h.seekToEnd()) ?? 0
    try? h.seek(toOffset: size > 4096 ? size - 4096 : 0)
    let tail = String(decoding: h.readDataToEndOfFile(), as: UTF8.self)
    l.recent = Array(tail.split(separator: "\n").map { line -> String in
      let t = String(line); let parts = t.split(separator: " ", maxSplits: 1)   // drop the date, keep "HH:MM:SS EVENT label"
      return parts.count == 2 && parts[0].count == 10 ? String(parts[1]) : t
    }.suffix(6).reversed())
    try? h.close()
  }
  return l
}
// manual_order: memguard's dispatcher reads ticket names one per line (text after a TAB is only a label) and lets the listed tickets
// swap among the places they already hold; priority ("0-" / priority_pattern) tickets never move. Hangar writes it when you drag a row.
let MANUAL_ORDER = MGDIR + "/manual_order"
func readManualOrder() -> [String] {
  ((try? String(contentsOfFile: MANUAL_ORDER, encoding: .utf8)) ?? "").split(separator: "\n").compactMap { l in
    let t = l.trimmingCharacters(in: .whitespaces)
    return t.isEmpty || t.hasPrefix("#") ? nil : String(t.split(whereSeparator: { $0 == " " || $0 == "\t" }).first ?? "")
  }
}
func writeManualOrder(heavy: [LaneJob], light: [LaneJob]) {
  let rows = (heavy + light).filter { !$0.priority }.map { "\($0.ticket)\t\($0.label)" }
  let body = "# Written by Hangar (Tower tab, drag to reorder). One ticket name per line (text after a TAB is only the label). Read by memguard.py dispatch().\n" + rows.joined(separator: "\n") + "\n"
  let tmp = MANUAL_ORDER + ".tmp"
  if (try? body.write(toFile: tmp, atomically: false, encoding: .utf8)) != nil { _ = rename(tmp, MANUAL_ORDER) }
}
func dur(_ s: Int) -> String { s >= 3600 ? "\(s / 3600)h \(s % 3600 / 60)m" : s >= 60 ? "\(s / 60)m" : "\(s)s" }

// ---------------------------------------------------------------- queue tab (Nov 4, 2026: the COO's agent queue, read-only)
// Reads the newest AGENT_QUEUE_*.md and shows running count, queued count, the top 3 queued items and how old the file is
// (amber when older than 45 min; the file is refreshed about every 30 min). Parses the markdown tables under "## Running" and "## Queued":
// rows whose first cell is R<id> / Q<id>; struck-through ~~Q0~~ rows are skipped. Read-only, no network, re-read at most every 60 s.
// HANGAR_QUEUE_FILE is a file path or a glob such as ~/notes/AGENT_QUEUE_*.md (newest name wins). Unset = no Queue tab.
let QUEUE_PATTERN: String? = ENV["HANGAR_QUEUE_FILE"]
let QUEUE_STALE_SECS = 45 * 60
let QUEUE_MIN_REREAD = 60.0
struct QItem: Identifiable { let id: String; let text: String }
struct QueueInfo { var found = false, name = "", running = 0, queued = 0, top = [QItem](), mtime: Date? = nil }
func queueCells(_ line: String) -> [String] {
  var c = line.split(separator: "|", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
  if c.first == "" { c.removeFirst() }
  if c.last == "" { c.removeLast() }
  return c
}
func queueClean(_ s: String) -> String { s.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "") }
func isQueueId(_ s: String, _ letter: Character) -> Bool {
  guard s.first == letter, s.count >= 2 else { return false }
  return s.dropFirst().allSatisfy { $0.isLetter || $0.isNumber }
}
func parseQueue(_ md: String) -> (running: Int, queued: [QItem]) {
  var sect = 0, running = 0, queued = [QItem]()
  for raw in md.split(separator: "\n", omittingEmptySubsequences: true) {
    let line = raw.trimmingCharacters(in: .whitespaces)
    if line.hasPrefix("#") {
      let l = line.lowercased()
      sect = l.hasPrefix("## ") && l.contains("running") ? 1 : (l.hasPrefix("## ") && l.contains("queued") ? 2 : 0)
      continue
    }
    guard sect != 0, line.hasPrefix("|") else { continue }
    let c = queueCells(line)
    guard c.count >= 2, !c[1].hasPrefix("~~") else { continue }   // struck-through rows start with ~~ and fail the id test too
    if sect == 1, isQueueId(c[0], "R") { running += 1 }
    if sect == 2, isQueueId(c[0], "Q") { queued.append(QItem(id: c[0], text: queueClean(c[1]))) }
  }
  return (running, queued)
}
func newestQueueFile(_ pattern: String) -> String? {
  let p = (pattern as NSString).expandingTildeInPath
  guard p.contains("*") || p.contains("?") else { return FileManager.default.fileExists(atPath: p) ? p : nil }
  let dir = (p as NSString).deletingLastPathComponent, pat = (p as NSString).lastPathComponent
  let names = ((try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []).filter { fnmatch(pat, $0, 0) == 0 }.sorted()
  return names.last.map { dir + "/" + $0 }   // dated names (AGENT_QUEUE_2026-11-04.md) sort oldest to newest
}
func readQueue() -> QueueInfo {
  guard let pat = QUEUE_PATTERN, let path = newestQueueFile(pat), let text = try? String(contentsOfFile: path, encoding: .utf8) else { return QueueInfo() }
  let r = parseQueue(text)
  let m = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
  return QueueInfo(found: true, name: (path as NSString).lastPathComponent, running: r.running, queued: r.queued.count, top: Array(r.queued.prefix(3)), mtime: m)
}

struct Status {
  var sc = false, scLeft = 0, scMode = "scramble", scTarget = 20, scDiskLow = false, scDiskGB: Double? = nil   // Nov 4: CAP 12 / Scramble 20 / Surge 25 / Custom 1-40
  var memFree: Int? = nil, load = 0.0, ab = false, abLeft = 0, drive: String? = nil, driveWhy = "", diskFree: Double? = nil
  var lastTidy: Double? = nil, lastSave: Double? = nil, lastBackup: Double? = nil, loaded = false
}
func parseStatus(_ s: String) -> Status? {
  guard let line = s.split(separator: "\n").last(where: { $0.hasPrefix("{") }), let d = line.data(using: .utf8),
        let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return nil }
  var st = Status(); st.loaded = true
  st.memFree = j["mem_free_pct"] as? Int; st.load = j["load1"] as? Double ?? 0
  st.ab = j["afterburner"] as? Bool ?? false; st.abLeft = j["afterburner_left_min"] as? Int ?? 0
  st.drive = j["drive"] as? String; st.diskFree = j["disk_free_gb"] as? Double
  if let sk = j["drives_skipped"] as? [String: String] {
    st.driveWhy = sk.filter { !$0.key.hasPrefix(".") && !$0.key.hasPrefix("com.apple") && $0.key != "Macintosh HD" }
      .map { "\($0.key): \($0.value)" }.sorted().joined(separator: "\n")
  }
  st.lastTidy = j["last_tidy"] as? Double; st.lastSave = j["last_save"] as? Double; st.lastBackup = j["last_backup"] as? Double
  return st
}

enum Act { case afterburner, scramble, tidy, save, backup, all
  var title: String { switch self { case .afterburner: return "Afterburner"; case .scramble: return "Scramble"; case .tidy: return "Jettison"; case .save: return "Wheels Up"
    case .backup: return "Black Box"; case .all: return "Alpha Strike" } }   // jet call-signs; Alpha Strike = every aircraft launches together
  var tidyArgs: [String] { switch self { case .tidy: return ["--only", "clean"]; case .save: return ["--only", "save"]
    case .backup: return ["--only", "backup"]; default: return [] } }
}
struct Pending { let act: Act; let plan: String; let summary: String }

final class Model: ObservableObject {
  @Published var st = Status()
  @Published var busy: Act? = nil
  @Published var busyNote = ""
  @Published var result = ""
  @Published var pending: Pending? = nil
  @Published var loginOn = SMAppService.mainApp.status == .enabled
  @Published var tab = 0
  @Published var scPick = "scramble"   // mode a tap launches when Scramble is off
  @Published var customN = 20
  @Published var flySound = FlySettings.sound
  @Published var animOn = FlySettings.animations
  @Published var lanes = Lanes()
  @Published var queue = QueueInfo()
  var queueRead = Date.distantPast
  @Published var queueTick = Date()   // bumped on every read so the "updated N min ago" line redraws
  func moveQueued(heavy: Bool, from: IndexSet, to: Int) {
    if heavy { lanes.heavyQ.move(fromOffsets: from, toOffset: to) } else { lanes.lightQ.move(fromOffsets: from, toOffset: to) }
    writeManualOrder(heavy: lanes.heavyQ, light: lanes.lightQ)
    result = "Queue order saved; memguard uses it on its next pass (priority jobs stay first)."
  }
  /// Re-reads the agent queue file at most once a minute (force skips the wait); off the main thread, no network.
  func refreshQueue(force: Bool = false) {
    guard QUEUE_PATTERN != nil, force || Date().timeIntervalSince(queueRead) >= QUEUE_MIN_REREAD else { return }
    queueRead = Date()
    DispatchQueue.global(qos: .utility).async { let q = readQueue(); DispatchQueue.main.async { self.queue = q; self.queueTick = Date() } }
  }
  func refreshLanes() { DispatchQueue.global(qos: .utility).async { let l = readLanes(); DispatchQueue.main.async { self.lanes = l } } }

  func refresh() {
    DispatchQueue.global(qos: .userInitiated).async {
      let (_, out) = runTool(TIDY, ["--status"])
      let (_, so) = runTool(SCRAMBLE, ["status"])
      var sc = false, scLeft = 0, mode = "scramble", target = 20, low = false, gb: Double? = nil
      if let line = so.split(separator: "\n").last(where: { $0.hasPrefix("{") }), let d = line.data(using: .utf8),
         let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
        sc = j["scramble"] as? Bool ?? false; scLeft = j["scramble_left_min"] as? Int ?? 0
        mode = j["mode"] as? String ?? "scramble"; target = j["target_agents"] as? Int ?? 20
        low = j["disk_low"] as? Bool ?? false; gb = j["disk_free_gb"] as? Double
      }
      if var s = parseStatus(out) {
        s.sc = sc; s.scLeft = scLeft; s.scMode = mode; s.scTarget = target; s.scDiskLow = low; s.scDiskGB = gb
        DispatchQueue.main.async { self.st = s; if sc { self.scPick = mode; if mode == "custom" { self.customN = target } } }
      }
    }
    refreshLanes()
  }
  func tap(_ a: Act) {
    guard busy == nil else { return }
    switch a {
    case .afterburner:
      if !st.ab { Flyover.play(.afterburner) }   // F-22 streak and sonic boom, on switching ON only
      run(a, AFTERBURNER, st.ab ? ["off", "turned off in Hangar"] : ["on", "2h", "Hangar button"])
    case .scramble: if st.sc { run(a, SCRAMBLE, ["off", "turned off in Hangar"]) } else { setScramble(scPick) }
    case .save: Flyover.play(.wheels); run(a, TIDY, a.tidyArgs)   // carrier launch, gear up
    case .tidy, .backup, .all: preview(a)
    }
  }
  /// Scramble mode picker: cap | scramble | surge | cas | custom (uses customN). The CLI refuses counts above 20 under 10 GB free and says why.
  func setScramble(_ mode: String) {
    guard busy == nil else { return }
    scPick = mode
    let why = ["Hangar button"]
    switch mode {
    case "cap": run(.scramble, SCRAMBLE, ["cap", "8h"] + why)
    case "surge": run(.scramble, SCRAMBLE, ["surge", "8h"] + why)
    case "cas": run(.scramble, SCRAMBLE, ["cas", "8h"] + why)   // Nov 4: 30 agents; the dive animation fires from the state-file watcher
    case "custom": run(.scramble, SCRAMBLE, ["set", "\(min(40, max(1, customN)))", "8h"] + why)
    default: run(.scramble, SCRAMBLE, ["on", "8h"] + why)
    }
  }
  func preview(_ a: Act) {
    busy = a; busyNote = "Checking what \(a.title.lowercased()) would do…"; result = ""
    DispatchQueue.global(qos: .userInitiated).async {
      let (_, out) = runTool(TIDY, ["--dry-run"] + a.tidyArgs)
      DispatchQueue.main.async { self.busy = nil; self.pending = Pending(act: a, plan: out.trimmingCharacters(in: .whitespacesAndNewlines), summary: summaryLine(out)) }
    }
  }
  func confirm() {
    guard let p = pending else { return }; pending = nil
    Flyover.play(p.act == .tidy ? .jettison : p.act == .backup ? .blackbox : .alpha)   // drop tank / recorder / three-ship V (Alpha Strike is one combined pass)
    run(p.act, TIDY, p.act.tidyArgs)
  }
  func run(_ a: Act, _ tool: String, _ args: [String]) {
    busy = a; result = ""
    busyNote = (a == .backup || a == .all) ? "Working… the backup waits its turn in the memguard queue" : "Working…"
    DispatchQueue.global(qos: .userInitiated).async {
      let (rc, out) = runTool(tool, args)
      DispatchQueue.main.async { self.busy = nil; self.result = (rc == 0 ? "" : "Problem: ") + summaryLine(out); self.refresh() }
    }
  }
  func setAnim(_ on: Bool) { FlySettings.animations = on; animOn = FlySettings.animations }
  func setFlySound(_ on: Bool) { FlySettings.sound = on; flySound = FlySettings.sound }
  func setLogin(_ on: Bool) {
    do { if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() } }
    catch { result = "Open at login: \(error.localizedDescription)" }
    loginOn = SMAppService.mainApp.status == .enabled
  }
}

// ---------------------------------------------------------------- cheeky icons (vector, no image files)
struct JetShape: Shape {   // top-down fighter, nose up, in a 100 x 100 box
  func path(in r: CGRect) -> Path {
    let pts: [(CGFloat, CGFloat)] = [(50, 2), (53, 12), (55, 30), (90, 58), (90, 64), (60, 66), (62, 72), (76, 84), (76, 90), (58, 88), (56, 95),
                                     (44, 95), (42, 88), (24, 90), (24, 84), (38, 72), (40, 66), (10, 64), (10, 58), (45, 30), (47, 12)]
    var p = Path(); let s = min(r.width, r.height) / 100, ox = r.midX - 50 * s, oy = r.minY
    p.addLines(pts.map { CGPoint(x: ox + $0.0 * s, y: oy + $0.1 * s) }); p.closeSubpath()
    p.addEllipse(in: CGRect(x: ox + 47.5 * s, y: oy + 14 * s, width: 5 * s, height: 11 * s))   // canopy hole (even-odd)
    return p
  }
}
struct FlameShape: Shape {   // two afterburner plumes below the nozzles
  func path(in r: CGRect) -> Path {
    var p = Path(); let s = r.width / 100
    for x in [46.5, 53.5] as [CGFloat] {
      p.move(to: CGPoint(x: r.minX + (x - 3) * s, y: r.minY))
      p.addQuadCurve(to: CGPoint(x: r.minX + x * s, y: r.maxY), control: CGPoint(x: r.minX + (x - 4) * s, y: r.minY + r.height * 0.6))
      p.addQuadCurve(to: CGPoint(x: r.minX + (x + 3) * s, y: r.minY), control: CGPoint(x: r.minX + (x + 4) * s, y: r.minY + r.height * 0.6))
      p.closeSubpath()
    }
    return p
  }
}
struct Jet: View {
  var on: Bool
  var body: some View {
    VStack(spacing: 0) {
      JetShape().fill(on ? Color.primary : Color.secondary, style: FillStyle(eoFill: true)).frame(width: 34, height: 34)
      FlameShape().fill(LinearGradient(colors: [.white, .yellow, .orange, .red.opacity(0)], startPoint: .top, endPoint: .bottom))
        .frame(width: 34, height: 18).shadow(color: .orange, radius: 6).shadow(color: .red.opacity(0.6), radius: 10).opacity(on ? 1 : 0)
    }.offset(y: 8)
  }
}
// A-10 silhouette: an original vector (straight wing, twin engine pods, twin tails, nose gun) shown beside the plane while the 30-agent mode (Close Air Support, CAS) is on.
// Original vector, one geometry for everything (tile icon, menu-bar template image, flyover layer, screenshots): top-down planform in a
// 100 x 96 box, nose up, y down: nose gun barrel, straight wing, twin engine pods high on the rear fuselage, tailplane with a fin at each tip.
// Every piece winds the same way so the union fills solid under the non-zero rule (no holes where pieces overlap).
func a10Path(in r: CGRect, noseRight: Bool = false) -> CGPath {
  let p = CGMutablePath()
  func poly(_ pts: [(CGFloat, CGFloat)]) { p.addLines(between: pts.map { CGPoint(x: $0.0, y: $0.1) }); p.closeSubpath() }
  func rect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) { poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)]) }
  func pod(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ c: CGFloat) {
    poly([(x + c, y), (x + w - c, y), (x + w, y + c), (x + w, y + h - c), (x + w - c, y + h), (x + c, y + h), (x, y + h - c), (x, y + c)])
  }
  rect(48.6, 0, 2.8, 16)                                                                              // nose gun barrel
  poly([(50, 8), (54, 14), (56.5, 26), (57, 44), (57, 70), (53, 92), (47, 92), (43, 70), (43, 44), (43.5, 26), (46, 14)])      // fuselage and canopy section
  poly([(3, 43), (8, 40), (44, 36), (56, 36), (92, 40), (97, 43), (97, 53), (92, 55), (56, 58), (44, 58), (8, 55), (3, 53)])                       // long straight wing
  pod(33, 46, 11.5, 44, 5); pod(55.5, 46, 11.5, 44, 5)                                               // twin engine pods
  rect(22, 80, 56, 8)                                                                                  // tailplane
  rect(20.5, 73, 3.8, 21); rect(75.7, 73, 3.8, 21)                                                     // twin tail fins
  var t = noseRight ? CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 96, ty: 0) : .identity
  let w: CGFloat = noseRight ? 96 : 100, h: CGFloat = noseRight ? 100 : 96
  let s = min(r.width / w, r.height / h)
  t = t.concatenating(CGAffineTransform(scaleX: s, y: s)).concatenating(CGAffineTransform(translationX: r.midX - w * s / 2, y: r.midY - h * s / 2))
  return p.copy(using: &t) ?? p
}
struct A10Shape: Shape {
  var noseRight = false
  func path(in r: CGRect) -> Path { Path(a10Path(in: r, noseRight: noseRight)) }
}
/// The menu-bar image: the plane, and (while the mode is Close Air Support or the target is 30+) the A-10 beside it. One monochrome template image,
/// so it follows light and dark menu bars like any system icon.
let CAS_MIN_AGENTS = 30
func menuBarImage(symbol: String, a10: Bool) -> NSImage? {
  guard let sym = NSImage(systemSymbolName: symbol, accessibilityDescription: a10 ? "Hangar, Close Air Support mode" : "Hangar") else { return nil }
  if !a10 { sym.isTemplate = true; return sym }
  let h: CGFloat = 18, jw: CGFloat = 17, jh: CGFloat = 17, gap: CGFloat = 3, sw = sym.size.width, sh = sym.size.height
  let img = NSImage(size: NSSize(width: sw + gap + jw, height: h), flipped: false) { _ in
    sym.draw(in: NSRect(x: 0, y: (h - sh) / 2, width: sw, height: sh))
    guard let ctx = NSGraphicsContext.current?.cgContext else { return true }
    NSColor.black.setFill()
    ctx.addPath(a10Path(in: CGRect(x: sw + gap, y: (h - jh) / 2, width: jw, height: jh), noseRight: true)); ctx.fillPath()
    return true
  }
  img.isTemplate = true; img.accessibilityDescription = "Hangar, Close Air Support mode"
  return img
}

// ---------------------------------------------------------------- screen animations (Nov 4)
// Screen animations: the A-10 dive for Close Air Support (in from the top-left, nose down, gun burst, fire and booms at the bottom-right), then one animation per Hangar action. Each one plays once per action.
// Hangar action. Every scene is drawn here (vector shapes + CoreAnimation) with sound synthesised here (a small sample-by-sample mixer): no image
// files, no recorded or downloaded audio. Overlay = borderless, transparent, click-through window at screen-saver level on the main screen;
// it never activates the app or takes focus. Reduce Motion skips everything (animation and sound). The "Animations" and "Flyover sound"
// switches in the panel turn the pictures and the sound off; sound follows the system volume and mute. Each scene plays once per action.
//   dive (Close Air Support 30)  afterburner (F-22 sonic boom)  jettison (drop tank)  wheels (carrier launch, gear up)  blackbox (recorder + beacon)
//   alpha (3-ship V)   cap (2 ships)  scramble (4)  surge (6)
// Demo: HANGAR_FLYOVER=<scene> <Hangar binary> plays one and exits (1 = dive); HANGAR_FLYOVER_BACKDROP=1 adds an opaque sky/ground so it can be
// filmed without the desktop; HANGAR_FLYOVER_SOUND=off mutes it; HANGAR_FLYOVER_WAV=<file> writes the scene's sound to a WAV and exits.
enum FlySettings {
  static var sound: Bool {
    get { if let e = ENV["HANGAR_FLYOVER_SOUND"], ["off", "0", "no"].contains(e.lowercased()) { return false }
          return (UserDefaults.standard.object(forKey: "flyoverSound") as? Bool) ?? true }   // on by default
    set { UserDefaults.standard.set(newValue, forKey: "flyoverSound") }
  }
  static var animations: Bool {
    get { (UserDefaults.standard.object(forKey: "animations") as? Bool) ?? true }
    set { UserDefaults.standard.set(newValue, forKey: "animations") }
  }
}
enum FlyScene: String { case dive, afterburner, jettison, wheels, blackbox, alpha, cap, scramble, surge }

// --- sound: voices are small stateful closures of time; the mixer sums them and soft-clips
var fnState: UInt64 = 0x9E3779B97F4A7C15
func fnoise() -> Double { fnState ^= fnState << 13; fnState ^= fnState >> 7; fnState ^= fnState << 17; return Double(fnState % 20001) / 10000.0 - 1.0 }
let SR = 44100.0, TAU = 2 * Double.pi
typealias Voice = (Double) -> Double
func trapezoid(_ t: Double, _ t0: Double, _ t1: Double, _ up: Double, _ down: Double) -> Double { max(0, min(1, (t - t0) / up, (t1 - t) / down)) }
/// band-passed noise sweeping up to the pass at `tc` and back down (the doppler swoosh), with a thin whine
func vWhoosh(_ tc: Double, _ pre: Double, _ post: Double, _ lo: Double, _ hi: Double, _ amp: Double) -> Voice {
  var low = 0.0, band = 0.0, ph = 0.0
  return { t in
    let d = t - tc, w = exp(-pow(d / (d < 0 ? pre : post), 2))
    if w < 0.002 { return 0 }
    let f = 2 * sin(Double.pi * (lo + (hi - lo) * w) / SR), h = fnoise() - low - 0.45 * band
    band += f * h; low += f * band
    ph += TAU * (hi * 0.6 - hi * 0.25 * max(0, min(1, (d + 0.2) / 0.6))) / SR
    return amp * w * (band * 0.55 + sin(ph) * 0.05)
  }
}
/// ~67 rounds a second: each round a low thump plus a noise growl
func vGun(_ t0: Double, _ t1: Double, _ amp: Double) -> Voice {
  var clock = 1.0, age = 1.0, a = 1.0, nz = 0.0, out = 0.0, period = 0.015
  return { t in
    if t >= t0 && t <= t1 { clock += 1 / SR; if clock >= period { clock = 0; age = 0; a = 0.8 + 0.2 * abs(fnoise()); period = (1 / 67.0) * (1 + 0.07 * fnoise()) } }
    var g = 0.0
    if age < 0.05 { nz += 0.35 * (fnoise() - nz); g = exp(-age / 0.0045) * (0.8 * sin(TAU * 85 * age) + 0.9 * nz) * a; age += 1 / SR }
    out += 0.6 * (g - out)
    return out * amp * 0.5
  }
}
func vBoom(_ t0: Double, _ amp: Double) -> Voice {
  var ph = 0.0, lp = 0.0
  return { t in
    let a = t - t0
    guard a >= 0 && a < 1.3 else { return 0 }
    ph += TAU * (36 + 44 * exp(-a / 0.12)) / SR; lp += 0.05 * (fnoise() - lp)
    return amp * 0.5 * (sin(ph) * exp(-a / 0.38) + 3.5 * lp * exp(-a / 0.25)) * (1 - exp(-a / 0.004))
  }
}
func vThump(_ t0: Double, _ f: Double, _ amp: Double, _ dec: Double) -> Voice {
  return { t in
    let a = t - t0
    guard a >= 0 && a < dec * 8 else { return 0 }
    return amp * exp(-a / dec) * sin(TAU * f * (1 + 0.6 * exp(-a / 0.02)) * a) + 0.25 * amp * fnoise() * exp(-a / 0.003)
  }
}
func vCrack(_ t0: Double, _ amp: Double) -> Voice {   // one crack of a sonic boom: sharp noise snap over a low thud
  var lp = 0.0
  return { t in
    let a = t - t0
    guard a >= 0 && a < 0.6 else { return 0 }
    let n = fnoise(); lp += 0.25 * (n - lp)
    return amp * (1.3 * (n - lp) * exp(-a / 0.012) + 0.8 * sin(TAU * (48 + 60 * exp(-a / 0.05)) * a) * exp(-a / 0.15))
  }
}
func vBeep(_ t0: Double, _ f: Double, _ dur: Double, _ amp: Double) -> Voice {   // gentle locator beep
  return { t in
    let a = t - t0
    guard a >= 0 && a < dur * 2 else { return 0 }
    return amp * (1 - exp(-a / 0.008)) * exp(-a / (dur / 3)) * (sin(TAU * f * a) + 0.25 * sin(TAU * 2 * f * a))
  }
}
func vHiss(_ t0: Double, _ t1: Double, _ amp: Double) -> Voice {   // steam
  var lp = 0.0
  return { t in
    let e = trapezoid(t, t0, t1, 0.08, 0.25); if e <= 0 { return 0 }
    let n = fnoise(); lp += 0.2 * (n - lp); return amp * e * (n - lp) * 0.5
  }
}
func vRoar(_ t0: Double, _ t1: Double, _ f0: Double, _ f1: Double, _ amp: Double) -> Voice {   // engine spool: rising noise + a thin saw
  var lp = 0.0, ph = 0.0, saw = 0.0
  return { t in
    let e = trapezoid(t, t0, t1, 0.25, 0.3); if e <= 0 { return 0 }
    let u = max(0, min(1, (t - t0) / (t1 - t0))), k = min(0.9, TAU * (f0 + (f1 - f0) * u) / SR)
    lp += k * (fnoise() - lp); ph += (70 + 140 * u) / SR; saw += 0.15 * ((2 * (ph - floor(ph)) - 1) - saw)
    return amp * e * (2.2 * lp + 0.35 * saw)
  }
}
final class FlySynth {
  let voices: [Voice], seconds: Double
  var n = 0
  init(_ v: [Voice], _ secs: Double) { voices = v; seconds = secs }
  func next() -> Float {
    let t = Double(n) / SR; n += 1
    var x = 0.0
    for v in voices { x += v(t) }
    return Float(tanh(x * 0.9) * min(1, max(0, (seconds - t) / 0.3)) * 0.8)
  }
  func writeWav(_ path: String) {
    let frames = Int(seconds * SR); var pcm = [Int16](); pcm.reserveCapacity(frames)
    for _ in 0..<frames { pcm.append(Int16(max(-1, min(1, Double(next()))) * 32767)) }
    var h = Data(); func u32(_ v: UInt32) { var x = v.littleEndian; h.append(Data(bytes: &x, count: 4)) }; func u16(_ v: UInt16) { var x = v.littleEndian; h.append(Data(bytes: &x, count: 2)) }
    h.append("RIFF".data(using: .ascii)!); u32(UInt32(36 + frames * 2)); h.append("WAVEfmt ".data(using: .ascii)!)
    u32(16); u16(1); u16(1); u32(44100); u32(88200); u16(2); u16(16); h.append("data".data(using: .ascii)!); u32(UInt32(frames * 2))
    pcm.withUnsafeBufferPointer { h.append(Data(buffer: $0)) }
    try? h.write(to: URL(fileURLWithPath: path))
  }
}

// --- drawing helpers (all original vector art; the fighter is the same JetShape as the Afterburner tile icon)
func fighterCGPath(_ size: CGFloat) -> CGPath {   // JetShape turned nose-right for CoreAnimation (y up); symmetric, so no mirror issue
  let t = CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 100, ty: 0).concatenating(CGAffineTransform(scaleX: size / 100, y: size / 100))
  let src = JetShape().path(in: CGRect(x: 0, y: 0, width: 100, height: 100)).cgPath
  return src.copy(using: [t]) ?? src
}
func cg(_ r: Double, _ g: Double, _ b: Double, _ a: Double = 1) -> CGColor { CGColor(red: r, green: g, blue: b, alpha: a) }
func shaped(_ path: CGPath, _ size: CGSize, fill: CGColor?, stroke: CGColor? = nil, lw: CGFloat = 0, evenOdd: Bool = false) -> CAShapeLayer {
  let l = CAShapeLayer(); l.bounds = CGRect(origin: .zero, size: size); l.anchorPoint = .zero; l.position = .zero; l.path = path
  l.fillColor = fill; l.strokeColor = stroke; l.lineWidth = lw; if evenOdd { l.fillRule = .evenOdd }; return l
}
func dot(_ d: CGFloat, _ color: CGColor?) -> CAShapeLayer {   // circle centred on its position, hidden until animated
  let l = CAShapeLayer(); l.bounds = CGRect(x: 0, y: 0, width: d, height: d); l.path = CGPath(ellipseIn: l.bounds, transform: nil); l.fillColor = color; l.opacity = 0; return l
}
func ring(_ d: CGFloat, _ color: CGColor, _ lw: CGFloat) -> CAShapeLayer {
  let l = CAShapeLayer(); l.bounds = CGRect(x: 0, y: 0, width: d, height: d); l.path = CGPath(ellipseIn: l.bounds.insetBy(dx: lw, dy: lw), transform: nil)
  l.fillColor = nil; l.strokeColor = color; l.lineWidth = lw; l.opacity = 0; return l
}
func flame(_ x0: CGFloat, _ y: CGFloat, _ len: CGFloat, _ r: CGFloat, _ color: CGColor) -> CAShapeLayer {   // afterburner plume pointing left from a nozzle
  let p = CGMutablePath()
  p.move(to: CGPoint(x: x0, y: y + r)); p.addQuadCurve(to: CGPoint(x: x0 - len, y: y), control: CGPoint(x: x0 - len * 0.4, y: y + r * 1.2))
  p.addQuadCurve(to: CGPoint(x: x0, y: y - r), control: CGPoint(x: x0 - len * 0.4, y: y - r * 1.2)); p.closeSubpath()
  let l = CAShapeLayer(); l.path = p; l.fillColor = color; l.bounds = CGRect(x: 0, y: 0, width: x0, height: y * 2); l.anchorPoint = CGPoint(x: 1, y: 0.5)
  l.position = CGPoint(x: x0, y: y)
  let a = CABasicAnimation(keyPath: "transform.scale.x"); a.fromValue = 0.8; a.toValue = 1.25; a.duration = 0.045; a.autoreverses = true; a.repeatCount = .infinity
  l.add(a, forKey: "flicker"); return l
}
/// A fighter: dark body with a tinted edge, twin afterburner plumes behind it. Size is the length (and wingspan) of the 100-box.
func fighterLayer(_ size: CGFloat, tint: CGColor, plumes: Bool = true) -> CALayer {
  let box = CALayer(); box.bounds = CGRect(x: 0, y: 0, width: size, height: size)
  let s = size / 100
  if plumes {
    for yy in [46.5, 53.5] as [CGFloat] {
      box.addSublayer(flame(5 * s, yy * s, 62 * s, 3.4 * s, cg(1, 0.45, 0.05, 0.9)))
      box.addSublayer(flame(5 * s, yy * s, 34 * s, 1.8 * s, cg(1, 0.95, 0.7, 1)))
    }
  }
  let body = shaped(fighterCGPath(size), box.bounds.size, fill: cg(0.16, 0.18, 0.21), stroke: tint, lw: 1.6, evenOdd: true)
  body.shadowColor = tint; body.shadowOpacity = 0.6; body.shadowRadius = 6; body.shadowOffset = .zero
  box.addSublayer(body); return box
}
func tank(_ s: CGFloat) -> CALayer {   // the drop tank of the Jettison tile, top-down
  let box = CALayer(); box.bounds = CGRect(x: 0, y: 0, width: 62 * s, height: 26 * s)
  let fin = CGMutablePath(); fin.addLines(between: [CGPoint(x: 0, y: 3 * s), CGPoint(x: 14 * s, y: 8 * s), CGPoint(x: 14 * s, y: 18 * s), CGPoint(x: 0, y: 23 * s)]); fin.closeSubpath()
  let f = shaped(fin, box.bounds.size, fill: cg(0.5, 0.38, 0.25)); box.addSublayer(f)
  let body = shaped(CGPath(ellipseIn: CGRect(x: 8 * s, y: 0, width: 54 * s, height: 26 * s), transform: nil), box.bounds.size, fill: cg(0.82, 0.66, 0.40), stroke: cg(0.45, 0.33, 0.2), lw: 1.2)
  box.addSublayer(body)
  let band = shaped(CGPath(rect: CGRect(x: 30 * s, y: 1.5 * s, width: 5 * s, height: 23 * s), transform: nil), box.bounds.size, fill: cg(0.5, 0.38, 0.25, 0.8)); box.addSublayer(band)
  return box
}
func recorder(_ s: CGFloat) -> CALayer {   // the orange flight recorder of the Black Box tile
  let box = CALayer(); box.bounds = CGRect(x: 0, y: 0, width: 80 * s, height: 70 * s)
  let orange = cg(1, 0.48, 0.10)
  let handle = shaped(CGPath(roundedRect: CGRect(x: 24 * s, y: 40 * s, width: 32 * s, height: 22 * s), cornerWidth: 6 * s, cornerHeight: 6 * s, transform: nil), box.bounds.size, fill: nil, stroke: orange, lw: 5 * s)
  let body = shaped(CGPath(roundedRect: CGRect(x: 6 * s, y: 4 * s, width: 68 * s, height: 44 * s), cornerWidth: 9 * s, cornerHeight: 9 * s, transform: nil), box.bounds.size, fill: orange)
  body.shadowColor = cg(0, 0, 0); body.shadowOpacity = 0.3; body.shadowRadius = 6; body.shadowOffset = CGSize(width: 0, height: -5)
  box.addSublayer(handle); box.addSublayer(body)
  for k in 0..<2 { box.addSublayer(shaped(CGPath(rect: CGRect(x: 6 * s, y: (14 + CGFloat(k) * 17) * s, width: 68 * s, height: 5.5 * s), transform: nil), box.bounds.size, fill: cg(1, 1, 1, 0.9))) }
  return box
}
func timed(_ a: CAAnimation, _ t0: CFTimeInterval, _ delay: Double, appear: Bool = false) -> CAAnimation {
  a.beginTime = t0 + delay; a.fillMode = appear ? .forwards : .both; a.isRemovedOnCompletion = false; return a
}
func fade(_ l: CALayer, _ t0: CFTimeInterval, _ from: Double, _ vals: [Double], _ keys: [NSNumber], _ dur: Double) {
  let a = CAKeyframeAnimation(keyPath: "opacity"); a.values = vals; a.keyTimes = keys; a.duration = dur
  l.add(timed(a, t0, from), forKey: "fade")
}
/// moves `l` along `path` at constant speed (rotating with the path if `turn`), starting `delay` s in
func flyAlong(_ l: CALayer, _ path: CGPath, _ dur: Double, _ delay: Double, _ t0: CFTimeInterval, turn: Bool, timing: CAMediaTimingFunction? = nil, key: String = "fly", appear: Bool = false) {
  let a = CAKeyframeAnimation(keyPath: "position"); a.path = path; a.duration = dur; a.calculationMode = .paced
  if turn { a.rotationMode = .rotateAuto }
  a.timingFunction = timing ?? CAMediaTimingFunction(name: .linear)
  l.position = CGPoint(x: -2000, y: -2000)
  l.add(timed(a, t0, delay, appear: appear), forKey: key)
}
func line(_ a: CGPoint, _ b: CGPoint) -> CGPath { let p = CGMutablePath(); p.move(to: a); p.addLine(to: b); return p }
func fireball(_ root: CALayer, _ c: CGPoint, _ size: CGFloat, _ t0: CFTimeInterval, _ ft: Double, _ s: CGFloat) {   // fireball + smoke + sparks
  let fb = CAGradientLayer(); fb.type = .radial; fb.bounds = CGRect(x: 0, y: 0, width: size, height: size); fb.position = c
  fb.colors = [cg(1, 1, 0.85), cg(1, 0.85, 0.2), cg(1, 0.42, 0.05, 0.9), cg(0.7, 0.1, 0, 0)]
  fb.locations = [0, 0.22, 0.58, 1]; fb.startPoint = CGPoint(x: 0.5, y: 0.5); fb.endPoint = CGPoint(x: 1, y: 1); fb.opacity = 0
  let sc = CABasicAnimation(keyPath: "transform.scale"); sc.fromValue = 0.15; sc.toValue = 1.5; sc.duration = 0.8; sc.timingFunction = CAMediaTimingFunction(name: .easeOut)
  let op = CAKeyframeAnimation(keyPath: "opacity"); op.values = [0, 1, 0.85, 0]; op.keyTimes = [0, 0.1, 0.5, 1]; op.duration = 0.8
  let g = CAAnimationGroup(); g.animations = [sc, op]; g.duration = 0.8
  fb.add(timed(g, t0, ft), forKey: "boom"); root.addSublayer(fb)
  for k in 0..<2 {
    let sm = dot(CGFloat(70 + k * 25) * s, cg(0.22, 0.22, 0.22, 0.6)); sm.position = CGPoint(x: c.x + CGFloat(k * 28 - 14) * s, y: c.y + 10 * s)
    let a1 = CABasicAnimation(keyPath: "transform.scale"); a1.fromValue = 0.5; a1.toValue = 2.4
    let a2 = CABasicAnimation(keyPath: "position.y"); a2.fromValue = sm.position.y; a2.toValue = sm.position.y + 110 * s
    let a3 = CAKeyframeAnimation(keyPath: "opacity"); a3.values = [0, 0.6, 0]; a3.keyTimes = [0, 0.25, 1]
    let gg = CAAnimationGroup(); gg.animations = [a1, a2, a3]; gg.duration = 1.5
    sm.add(timed(gg, t0, ft + 0.2 + Double(k) * 0.1), forKey: "smoke"); root.addSublayer(sm)
  }
  for k in 0..<10 {
    let sp = dot(4.5 * s, cg(1, 0.8 - CGFloat(k % 3) * 0.15, 0.2)); sp.position = c
    let th = Double(k) / 10 * TAU + Double(ft) * 3, r = CGFloat(110 + (k * 37) % 90) * s
    let p1 = CABasicAnimation(keyPath: "position"); p1.fromValue = c
    p1.toValue = CGPoint(x: c.x + CGFloat(cos(th)) * r, y: c.y + CGFloat(sin(th)) * r * 0.8 - 30 * s); p1.timingFunction = CAMediaTimingFunction(name: .easeOut)
    let p2 = CAKeyframeAnimation(keyPath: "opacity"); p2.values = [0, 1, 0]; p2.keyTimes = [0, 0.1, 1]
    let sg = CAAnimationGroup(); sg.animations = [p1, p2]; sg.duration = 0.7
    sp.add(timed(sg, t0, ft), forKey: "spark"); root.addSublayer(sp)
  }
}
func puff(_ root: CALayer, _ c: CGPoint, _ d: CGFloat, _ color: CGColor, _ t0: CFTimeInterval, _ at: Double, rise: CGFloat, dur: Double) {   // steam / smoke puff
  let l = dot(d, color); l.position = c
  let a1 = CABasicAnimation(keyPath: "transform.scale"); a1.fromValue = 0.4; a1.toValue = 1.8
  let a2 = CABasicAnimation(keyPath: "position.y"); a2.fromValue = c.y; a2.toValue = c.y + rise
  let a3 = CAKeyframeAnimation(keyPath: "opacity"); a3.values = [0, 0.8, 0]; a3.keyTimes = [0, 0.2, 1]
  let g = CAAnimationGroup(); g.animations = [a1, a2, a3]; g.duration = dur
  l.add(timed(g, t0, at), forKey: "puff"); root.addSublayer(l)
}

// --- the scenes: each fills `root` with layers and returns its voices and length in seconds
struct Built { var voices: [Voice]; var seconds: Double }
enum Scenes {
  static func build(_ sc: FlyScene, _ f: CGSize, _ root: CALayer, _ t0: CFTimeInterval) -> Built {
    switch sc {
    case .dive: return dive(f, root, t0)
    case .afterburner: return afterburner(f, root, t0)
    case .jettison: return jettison(f, root, t0)
    case .wheels: return wheels(f, root, t0)
    case .blackbox: return blackbox(f, root, t0)
    case .alpha: return formation(f, root, t0, [(0, 0), (-95, 62), (-95, -62)], tint: cg(1, 0.35, 0.25), secs: 2.4)
    case .cap: return formation(f, root, t0, [(0, 0), (-105, -70)], tint: cg(0.35, 0.65, 1), secs: 2.2)
    case .scramble: return formation(f, root, t0, [(0, 0), (-95, 62), (-95, -62), (-190, 0)], tint: cg(0.3, 0.85, 0.45), secs: 2.3)
    case .surge: return formation(f, root, t0, [(0, 0), (-95, 62), (-95, -62), (-190, 124), (-190, 0), (-190, -124)], tint: cg(1, 0.62, 0.15), secs: 2.4)
    }
  }
  static func sizeScale(_ f: CGSize) -> CGFloat { max(0.7, min(1.4, f.height / 900)) }

  /// N fighters in formation sweep left to right across the upper middle of the screen, plumes lit (Alpha Strike, CAP, Scramble, Surge).
  static func formation(_ f: CGSize, _ root: CALayer, _ t0: CFTimeInterval, _ offs: [(CGFloat, CGFloat)], tint: CGColor, secs: Double) -> Built {
    let s = sizeScale(f), W = f.width, H = f.height, dur = secs - 0.4, size = 95 * s
    let back = (offs.map { -$0.0 }.max() ?? 0) * s
    for (dx, dy) in offs {
      let a = CGPoint(x: -size - back + dx * s, y: H * 0.56 + dy * s), b = CGPoint(x: W + size + dx * s + 40 * s, y: H * 0.64 + dy * s)
      let jet = fighterLayer(size, tint: tint)
      jet.shadowColor = cg(0, 0, 0); jet.shadowOpacity = 0.3
      root.addSublayer(jet)
      flyAlong(jet, line(a, b), dur, 0, t0, turn: true, timing: CAMediaTimingFunction(controlPoints: 0.35, 0.1, 0.8, 0.7))
    }
    let n = Double(offs.count), tc = dur * 0.62
    return Built(voices: [vRoar(0, dur, 140, 520, 0.35 + 0.07 * n), vWhoosh(tc, 0.5, 0.4, 300, 1700, 0.5 + 0.06 * n)], seconds: secs)
  }

  /// The A-10 comes in from the top-left on a dive, fires a burst, pulls up and goes; fire and booms bloom at the bottom-right.
  static func dive(_ f: CGSize, _ root: CALayer, _ t0: CFTimeInterval) -> Built {
    let W = f.width, H = f.height, s = sizeScale(f), scale = NSScreen.main?.backingScaleFactor ?? 2
    let p0 = CGPoint(x: -150, y: H + 90), p1 = CGPoint(x: W * 0.78, y: H * 0.22), c = CGPoint(x: W * 0.93, y: H * 0.10), p2 = CGPoint(x: W + 260, y: H * 0.42)
    let path = CGMutablePath(); path.move(to: p0); path.addLine(to: p1); path.addQuadCurve(to: p2, control: c)
    let l1 = hypot(p1.x - p0.x, p1.y - p0.y)
    var l2: CGFloat = 0, last = p1
    for k in 1...24 {
      let u = CGFloat(k) / 24, a = (1 - u) * (1 - u), b = 2 * (1 - u) * u, d = u * u
      let q = CGPoint(x: a * p1.x + b * c.x + d * p2.x, y: a * p1.y + b * c.y + d * p2.y); l2 += hypot(q.x - last.x, q.y - last.y); last = q
    }
    let dur = 2.3, ti = dur * Double(l1 / (l1 + l2)), impact = CGPoint(x: p1.x + 30, y: p1.y - 40)
    let fireT = (0..<4).map { ti - 0.1 + 0.2 * Double($0) }, boomT = [fireT[0], fireT[2], fireT[3]].map { $0 + 0.1 }
    let gunOn = 0.45, gunOff = ti + 0.05, lag = 0.15, total = ti + 2.0
    let offs: [(CGFloat, CGFloat)] = [(0, 0), (-120, 40), (100, -20), (-45, 95)]
    for (i, o) in offs.enumerated() { fireball(root, CGPoint(x: impact.x + o.0 * s, y: impact.y + o.1 * s), (i == 0 ? 190 : 150) * s, t0, fireT[i], s) }
    let jetW: CGFloat = 150, jetH: CGFloat = 156, trail: CGFloat = 190, ahead: CGFloat = 260, cw = trail + jetW + ahead
    let box = CALayer(); box.bounds = CGRect(x: 0, y: 0, width: cw, height: jetH); box.anchorPoint = CGPoint(x: (trail + jetW / 2) / cw, y: 0.5)
    let jet = shaped(a10Path(in: CGRect(x: 0, y: 0, width: jetW, height: jetH), noseRight: true), CGSize(width: jetW, height: jetH), fill: cg(0.16, 0.18, 0.21), stroke: cg(1, 1, 1, 0.55), lw: 1.2)
    jet.position = CGPoint(x: trail, y: 0); jet.shadowColor = cg(0, 0, 0); jet.shadowOpacity = 0.35; jet.shadowRadius = 8; jet.shadowOffset = CGSize(width: 0, height: -10)
    func burst(_ l: CALayer) { l.opacity = 0; fade(l, t0, gunOn, [0, 1, 1, 0], [0, 0.06, 0.92, 1], gunOff - gunOn) }
    let txt = CATextLayer(); txt.string = "BRRRT"; txt.font = "Helvetica-BoldOblique" as CFString; txt.fontSize = 36
    txt.foregroundColor = cg(1, 1, 1); txt.alignmentMode = .right; txt.contentsScale = scale
    txt.shadowColor = cg(0, 0, 0); txt.shadowOpacity = 0.55; txt.shadowRadius = 3; txt.shadowOffset = .zero
    txt.frame = CGRect(x: 0, y: jetH / 2 - 22, width: trail - 14, height: 44); burst(txt)
    let tr = CAShapeLayer(); tr.path = line(CGPoint(x: trail + jetW - 2, y: jetH / 2), CGPoint(x: cw, y: jetH / 2))
    tr.strokeColor = cg(1, 0.88, 0.35); tr.lineWidth = 3.5; tr.lineCap = .round; tr.lineDashPattern = [9, 26]; tr.frame = CGRect(x: 0, y: 0, width: cw, height: jetH)
    let ph = CABasicAnimation(keyPath: "lineDashPhase"); ph.fromValue = 0; ph.toValue = -350; ph.duration = 1.2; ph.repeatCount = .infinity; tr.add(ph, forKey: "run"); burst(tr)
    let gun = CALayer(); gun.backgroundColor = cg(1, 0.85, 0.3); gun.bounds = CGRect(x: 0, y: 0, width: 12, height: 7); gun.position = CGPoint(x: trail + jetW + 3, y: jetH / 2); gun.cornerRadius = 3.5
    let fl = CABasicAnimation(keyPath: "backgroundColor"); fl.fromValue = cg(1, 0.85, 0.3); fl.toValue = cg(1, 0.4, 0.1, 0.2); fl.duration = 0.03; fl.autoreverses = true; fl.repeatCount = .infinity
    gun.add(fl, forKey: "fl"); burst(gun)
    box.addSublayer(txt); box.addSublayer(tr); box.addSublayer(jet); box.addSublayer(gun); root.addSublayer(box)
    flyAlong(box, path, dur, 0, t0, turn: true)
    let voices: [Voice] = [vWhoosh(ti + lag * 0.3, 0.75, 0.35, 350, 1850, 0.5), vGun(gunOn + lag, gunOff + lag, 1.0)] + boomT.map { vBoom($0, 0.6) }
    return Built(voices: voices, seconds: total)
  }

  /// F-22 streak: afterburner plumes, a vapor collar swelling around it, then the collar pops with a sonic boom (two cracks).
  static func afterburner(_ f: CGSize, _ root: CALayer, _ t0: CFTimeInterval) -> Built {
    let W = f.width, H = f.height, s = sizeScale(f), size = 140 * s, dur = 1.7
    let a = CGPoint(x: -size - 20, y: H * 0.62), b = CGPoint(x: W + size + 20, y: H * 0.50)
    let jet = fighterLayer(size, tint: cg(1, 0.6, 0.15))
    // vapor collar: a white bell hugging the rear of the jet, swelling in, then bursting
    let bell = CGMutablePath(); let S = size
    bell.move(to: CGPoint(x: S * 0.66, y: S * 0.5)); bell.addQuadCurve(to: CGPoint(x: S * 0.06, y: S * 0.04), control: CGPoint(x: S * 0.42, y: S * 0.14))
    bell.addQuadCurve(to: CGPoint(x: S * 0.06, y: S * 0.96), control: CGPoint(x: -S * 0.1, y: S * 0.5)); bell.addQuadCurve(to: CGPoint(x: S * 0.66, y: S * 0.5), control: CGPoint(x: S * 0.42, y: S * 0.86))
    let cone = shaped(bell, CGSize(width: S, height: S), fill: cg(1, 1, 1, 0.5), stroke: cg(1, 1, 1, 0.85), lw: 1.5)
    cone.anchorPoint = CGPoint(x: 0.4, y: 0.5); cone.position = CGPoint(x: S * 0.4, y: S * 0.5); cone.opacity = 0
    let tPop = dur * Double((W * 0.62 + size + 20) / (W + 2 * size + 40))
    let grow = CABasicAnimation(keyPath: "transform.scale"); grow.fromValue = 0.3; grow.toValue = 1.15; grow.duration = tPop - 0.3
    let bloom = CAKeyframeAnimation(keyPath: "opacity"); bloom.values = [0, 0.9, 0.9, 0]; bloom.keyTimes = [0, 0.4, 0.97, 1]; bloom.duration = tPop - 0.3
    let gg = CAAnimationGroup(); gg.animations = [grow, bloom]; gg.duration = tPop - 0.3
    cone.add(timed(gg, t0, 0.3, appear: true), forKey: "cone"); jet.insertSublayer(cone, at: 0)
    root.addSublayer(jet)
    flyAlong(jet, line(a, b), dur, 0, t0, turn: true, timing: CAMediaTimingFunction(controlPoints: 0.3, 0.0, 0.9, 0.8))
    // the pop: two expanding shock rings and a flash where the jet was
    let u = Double((W * 0.62 + size + 20) / (W + 2 * size + 40)), pc = CGPoint(x: W * 0.62, y: a.y + (b.y - a.y) * CGFloat(u))
    // (timing curve is not linear, so find the screen position at tPop by sampling the same curve)
    let flash = CAGradientLayer(); flash.type = .radial; flash.bounds = CGRect(x: 0, y: 0, width: 260 * s, height: 260 * s); flash.position = pc
    flash.colors = [cg(1, 1, 1, 1), cg(1, 1, 1, 0.5), cg(1, 1, 1, 0)]; flash.locations = [0, 0.4, 1]; flash.startPoint = CGPoint(x: 0.5, y: 0.5); flash.endPoint = CGPoint(x: 1, y: 1); flash.opacity = 0
    let fs = CABasicAnimation(keyPath: "transform.scale"); fs.fromValue = 0.3; fs.toValue = 1.4; fs.duration = 0.3
    let fo = CAKeyframeAnimation(keyPath: "opacity"); fo.values = [0, 0.9, 0]; fo.keyTimes = [0, 0.2, 1]; fo.duration = 0.3
    let fg = CAAnimationGroup(); fg.animations = [fs, fo]; fg.duration = 0.3; flash.add(timed(fg, t0, tPop, appear: true), forKey: "flash"); root.addSublayer(flash)
    for k in 0..<2 {
      let r = ring(110 * s, cg(1, 1, 1, 0.95), 7); r.position = pc
      let rs = CABasicAnimation(keyPath: "transform.scale"); rs.fromValue = 0.3; rs.toValue = 9; rs.duration = 0.8; rs.timingFunction = CAMediaTimingFunction(name: .easeOut)
      let ro = CAKeyframeAnimation(keyPath: "opacity"); ro.values = [0, 0.9, 0]; ro.keyTimes = [0, 0.08, 1]; ro.duration = 0.7
      let rg = CAAnimationGroup(); rg.animations = [rs, ro]; rg.duration = 0.7; r.add(timed(rg, t0, tPop + 0.08 * Double(k), appear: true), forKey: "ring"); root.addSublayer(r)
    }
    return Built(voices: [vRoar(0, dur, 160, 600, 0.5), vWhoosh(dur * 0.7, 0.5, 0.35, 350, 2000, 0.45), vCrack(tPop, 1.0), vCrack(tPop + 0.12, 0.8)], seconds: 2.5)
  }

  /// A fighter crosses; the drop tank releases from under it, tumbles and falls off the bottom of the screen.
  static func jettison(_ f: CGSize, _ root: CALayer, _ t0: CFTimeInterval) -> Built {
    let W = f.width, H = f.height, s = sizeScale(f), size = 120 * s, dur = 2.0
    let y = H * 0.66, a = CGPoint(x: -size, y: y), b = CGPoint(x: W + size, y: y + 20 * s)
    let jet = fighterLayer(size, tint: cg(0.85, 0.65, 0.35), plumes: true); root.addSublayer(jet)
    flyAlong(jet, line(a, b), dur, 0, t0, turn: true, timing: CAMediaTimingFunction(controlPoints: 0.2, 0.1, 0.6, 0.9))
    let tr = 0.95, xr = W * 0.48, yr = y + 7 * s
    let t = tank(s); t.opacity = 0; t.position = CGPoint(x: xr, y: yr); root.addSublayer(t)
    let fallPath = CGMutablePath(); fallPath.move(to: CGPoint(x: xr, y: yr)); fallPath.addQuadCurve(to: CGPoint(x: xr + 150 * s, y: -80 * s), control: CGPoint(x: xr + 110 * s, y: yr - 30 * s))
    let fp = CAKeyframeAnimation(keyPath: "position"); fp.path = fallPath; fp.duration = 1.2; fp.timingFunction = CAMediaTimingFunction(controlPoints: 0.5, 0.0, 0.9, 0.7)
    let rot = CABasicAnimation(keyPath: "transform.rotation.z"); rot.fromValue = 0; rot.toValue = -3 * TAU; rot.duration = 1.2
    let op = CABasicAnimation(keyPath: "opacity"); op.fromValue = 1; op.toValue = 1; op.duration = 1.2
    let g = CAAnimationGroup(); g.animations = [fp, rot, op]; g.duration = 1.2; t.add(timed(g, t0, tr, appear: true), forKey: "fall")
    return Built(voices: [vRoar(0, dur, 150, 450, 0.35), vWhoosh(dur * 0.55, 0.5, 0.4, 300, 1500, 0.4), vThump(tr, 150, 0.55, 0.05), vThump(tr + 0.03, 420, 0.25, 0.02), vWhoosh(tr + 0.55, 0.3, 0.5, 600, 900, 0.18)], seconds: 2.5)
  }

  /// Carrier launch (seen from above): jet on the catapult, steam venting; slung off the bow, dips, climbs out, gear comes up.
  static func wheels(_ f: CGSize, _ root: CALayer, _ t0: CFTimeInterval) -> Built {
    let W = f.width, H = f.height, s = sizeScale(f), size = 100 * s, dy = H * 0.105
    let ship = CALayer(); ship.frame = CGRect(x: 0, y: 0, width: W, height: H * 0.26)
    let sea = CAGradientLayer(); sea.frame = ship.bounds; sea.colors = [cg(0.10, 0.28, 0.45, 0.0), cg(0.06, 0.20, 0.36, 0.55), cg(0.04, 0.15, 0.28, 0.75)]; sea.locations = [0, 0.4, 1]; ship.addSublayer(sea)
    let x0 = W * 0.05, x1 = W * 0.80, yb = H * 0.045, yt = H * 0.165
    let hull = CGMutablePath(); hull.move(to: CGPoint(x: x0, y: yb)); hull.addLine(to: CGPoint(x: x1 - 60 * s, y: yb)); hull.addLine(to: CGPoint(x: x1 + 40 * s, y: dy)); hull.addLine(to: CGPoint(x: x1 - 60 * s, y: yt)); hull.addLine(to: CGPoint(x: x0, y: yt)); hull.closeSubpath()
    ship.addSublayer(shaped(hull, ship.bounds.size, fill: cg(0.30, 0.32, 0.35), stroke: cg(0.75, 0.78, 0.82), lw: 2))
    ship.addSublayer(shaped(line(CGPoint(x: x0 + 20, y: dy), CGPoint(x: x1 + 10 * s, y: dy)), ship.bounds.size, fill: nil, stroke: cg(1, 0.85, 0.2, 0.9), lw: 3))   // catapult track
    let dash = shaped(line(CGPoint(x: x0 + 30, y: yb + 10), CGPoint(x: x1 - 80 * s, y: yb + 10)), ship.bounds.size, fill: nil, stroke: cg(1, 1, 1, 0.55), lw: 2); dash.lineDashPattern = [14, 12]; ship.addSublayer(dash)
    ship.addSublayer(shaped(CGPath(rect: CGRect(x: W * 0.42, y: yt - 2, width: 120 * s, height: 30 * s), transform: nil), ship.bounds.size, fill: cg(0.22, 0.24, 0.27), stroke: cg(0.75, 0.78, 0.82), lw: 1.5))   // island
    for k in 0..<4 {   // wake
      let w = shaped(line(CGPoint(x: x0 - 20, y: yb + 6 + CGFloat(k) * 28 * s), CGPoint(x: x0 - 220 * s + CGFloat(k) * 40 * s, y: yb - 8 + CGFloat(k) * 34 * s)), ship.bounds.size, fill: nil, stroke: cg(1, 1, 1, 0.55 - Double(k) * 0.1), lw: 4); w.lineCap = .round; ship.addSublayer(w)
    }
    ship.addSublayer(shaped(CGPath(ellipseIn: CGRect(x: x1 + 20 * s, y: dy - 14 * s, width: 70 * s, height: 28 * s), transform: nil), ship.bounds.size, fill: cg(1, 1, 1, 0.45)))   // bow wave
    ship.opacity = 0; fade(ship, t0, 0, [0, 1, 1, 0], [0, 0.08, 0.88, 1], 2.5); root.addSublayer(ship)
    let launch = 0.7, run = 0.5, climb = 1.3, a = CGPoint(x: W * 0.17, y: dy), bow = CGPoint(x: x1 + 20 * s, y: dy)
    let deckRun = line(a, bow)
    let outPath = CGMutablePath(); outPath.move(to: bow)
    outPath.addQuadCurve(to: CGPoint(x: W * 0.90, y: dy - 12 * s), control: CGPoint(x: W * 0.855, y: dy - 12 * s))
    outPath.addCurve(to: CGPoint(x: W + 200, y: H * 0.50), control1: CGPoint(x: W * 0.95, y: dy - 6 * s), control2: CGPoint(x: W * 1.0, y: H * 0.30))
    let box = CALayer(); box.bounds = CGRect(x: 0, y: 0, width: size, height: size)
    let jetBody = fighterLayer(size, tint: cg(0.4, 0.8, 1), plumes: true); jetBody.position = CGPoint(x: size / 2, y: size / 2); box.addSublayer(jetBody)
    let total = run + climb
    let sc = CAKeyframeAnimation(keyPath: "transform.scale"); sc.values = [1, 1, 0.93, 1.5]; sc.keyTimes = [0, NSNumber(value: run / total), NSNumber(value: (run + 0.25) / total), 1]; sc.duration = total
    jetBody.add(timed(sc, t0, launch), forKey: "grow")   // climbing out: it comes up toward the viewer
    let tg = launch + run + 0.7
    for (gx, gy) in [(0.78, 0.5), (0.42, 0.34), (0.42, 0.66)] as [(CGFloat, CGFloat)] {   // nose and main gear, folding away
      let w = dot(7 * s, cg(0.9, 0.9, 0.92)); w.opacity = 1; w.position = CGPoint(x: gx * size, y: gy * size); w.strokeColor = cg(0, 0, 0, 0.7); w.lineWidth = 1
      let gs = CABasicAnimation(keyPath: "transform.scale"); gs.fromValue = 1; gs.toValue = 0; gs.duration = 0.25
      let go = CABasicAnimation(keyPath: "opacity"); go.fromValue = 1; go.toValue = 0; go.duration = 0.25
      let gg = CAAnimationGroup(); gg.animations = [gs, go]; gg.duration = 0.25
      w.add(timed(gg, t0, tg, appear: true), forKey: "gear"); jetBody.addSublayer(w)
    }
    root.addSublayer(box)
    flyAlong(box, deckRun, run, launch, t0, turn: true, timing: CAMediaTimingFunction(controlPoints: 0.7, 0.0, 1.0, 0.8), key: "deck")   // slung: accelerates down the track
    flyAlong(box, outPath, climb, launch + run, t0, turn: true, key: "climb", appear: true)                                                  // dips off the bow, climbs out
    for k in 0..<9 {   // catapult steam along the track, venting before the shot
      let px = W * 0.14 + CGFloat(k) * 34 * s
      puff(root, CGPoint(x: px, y: dy + (k % 2 == 0 ? 14 : -14) * s), 40 * s, cg(1, 1, 1, 0.75), t0, 0.05 + Double(k) * 0.06, rise: 60 * s, dur: 0.9)
    }
    return Built(voices: [vHiss(0.05, 0.8, 0.7), vThump(launch, 60, 0.9, 0.09), vThump(launch + 0.02, 190, 0.4, 0.03), vRoar(0.2, 2.4, 130, 520, 0.5),
                          vWhoosh(launch + run + 0.1, 0.3, 0.5, 400, 1400, 0.3), vThump(tg + 0.05, 95, 0.55, 0.07), vThump(tg + 0.18, 85, 0.45, 0.06)], seconds: 2.7)
  }

  /// The flight recorder drops in, bounces, then pings its locator beacon with ripples.
  static func blackbox(_ f: CGSize, _ root: CALayer, _ t0: CFTimeInterval) -> Built {
    let W = f.width, H = f.height, s = sizeScale(f)
    let c = CGPoint(x: W * 0.5, y: H * 0.30), rec = recorder(1.9 * s); rec.opacity = 1
    rec.position = CGPoint(x: c.x, y: H + 120)
    let drop = CAKeyframeAnimation(keyPath: "position.y"); drop.values = [H + 120, c.y, c.y + 70 * s, c.y, c.y + 16 * s, c.y]; drop.keyTimes = [0, 0.42, 0.58, 0.74, 0.86, 1]; drop.duration = 1.1
    drop.timingFunctions = [CAMediaTimingFunction(name: .easeIn), CAMediaTimingFunction(name: .easeOut), CAMediaTimingFunction(name: .easeIn), CAMediaTimingFunction(name: .easeOut), CAMediaTimingFunction(name: .easeIn)]
    rec.add(timed(drop, t0, 0), forKey: "drop")
    let out = CAKeyframeAnimation(keyPath: "opacity"); out.values = [1, 1, 0]; out.keyTimes = [0, 0.88, 1]; out.duration = 3.0; rec.add(timed(out, t0, 0), forKey: "out")
    let led = dot(12 * s, cg(1, 0.2, 0.15)); led.position = CGPoint(x: 40 * 1.9 * s, y: 51 * 1.9 * s); led.opacity = 0; rec.addSublayer(led)
    root.addSublayer(rec)
    let pings = [1.2, 1.85, 2.5]
    for tp in pings {
      for k in 0..<2 {
        let r = ring(70 * s, cg(0.15, 0.75, 0.72, 0.95), 6); r.position = CGPoint(x: c.x, y: c.y + 30 * s)
        let rs = CABasicAnimation(keyPath: "transform.scale"); rs.fromValue = 0.3; rs.toValue = 8; rs.duration = 1.0; rs.timingFunction = CAMediaTimingFunction(name: .easeOut)
        let ro = CAKeyframeAnimation(keyPath: "opacity"); ro.values = [0, 0.9, 0]; ro.keyTimes = [0, 0.06, 1]; ro.duration = 1.0
        let rg = CAAnimationGroup(); rg.animations = [rs, ro]; rg.duration = 1.0; r.add(timed(rg, t0, tp + 0.18 * Double(k), appear: true), forKey: "ring"); root.addSublayer(r)
      }
      let l = CAKeyframeAnimation(keyPath: "opacity"); l.values = [0, 1, 0.2, 0]; l.keyTimes = [0, 0.05, 0.4, 1]; l.duration = 0.5; led.add(timed(l, t0, tp, appear: true), forKey: "led\(tp)")
    }
    var v: [Voice] = [vThump(1.1 * 0.42, 80, 0.8, 0.07), vThump(1.1 * 0.58, 90, 0.4, 0.05), vThump(1.1 * 0.74, 100, 0.25, 0.04)]
    v += pings.map { vBeep($0, 1240, 0.16, 0.30) }
    return Built(voices: v, seconds: 3.3)
  }
}

enum Flyover {
  static var win: NSWindow?
  static var engine: AVAudioEngine?
  static func playSound(_ synth: FlySynth) {
    let e = AVAudioEngine()
    guard let fmt = AVAudioFormat(standardFormatWithSampleRate: SR, channels: 2) else { return }
    let node = AVAudioSourceNode(format: fmt) { _, _, frames, abl -> OSStatus in
      let bufs = UnsafeMutableAudioBufferListPointer(abl)
      for i in 0..<Int(frames) { let v = synth.next(); for b in bufs { b.mData?.assumingMemoryBound(to: Float.self)[i] = v } }
      return noErr
    }
    e.attach(node); e.connect(node, to: e.mainMixerNode, format: fmt)
    e.mainMixerNode.outputVolume = 0.6   // moderate; the system volume and mute still rule
    do { try e.start() } catch { return }
    engine = e
    DispatchQueue.main.asyncAfter(deadline: .now() + synth.seconds + 0.3) { e.stop(); if engine === e { engine = nil } }
  }
  static func addBackdrop(_ root: CALayer) {   // filming / offscreen frames only: a plain sky and ground
    let g = CAGradientLayer(); g.frame = root.bounds
    g.colors = [cg(0.30, 0.29, 0.27), cg(0.42, 0.40, 0.36), cg(0.78, 0.87, 0.95), cg(0.36, 0.54, 0.80)]; g.locations = [0, 0.26, 0.30, 1]; root.addSublayer(g)
  }
  static var lastPlay = Date.distantPast
  /// Plays one scene once per user action (never from launch, refresh or a timer). `force` is the demo path (ignores the Animations switch and the
  /// 20 s rest between scenes); Reduce Motion always wins.
  static func play(_ sc: FlyScene, force: Bool = false, done: (() -> Void)? = nil) {
    if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion || win != nil || (!force && !FlySettings.animations) { done?(); return }
    if !force && Date().timeIntervalSince(lastPlay) < 20 { done?(); return }   // safety stop: at most one scene per 20 s, whatever triggers it
    lastPlay = Date()
    guard let scr = NSScreen.main ?? NSScreen.screens.first else { done?(); return }
    let f = scr.frame
    let w = NSWindow(contentRect: f, styleMask: .borderless, backing: .buffered, defer: false)
    let backdrop = ENV["HANGAR_FLYOVER_BACKDROP"] != nil
    w.isOpaque = backdrop; w.backgroundColor = .clear; w.hasShadow = false; w.ignoresMouseEvents = true; w.isReleasedWhenClosed = false
    w.level = .screenSaver
    w.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
    let v = NSView(frame: NSRect(origin: .zero, size: f.size)); v.wantsLayer = true; w.contentView = v
    let root = v.layer!
    if backdrop { Flyover.addBackdrop(root) }
    let built = Scenes.build(sc, f.size, root, CACurrentMediaTime())
    win = w; w.orderFrontRegardless()   // never makeKey / activate: focus stays where it was
    if FlySettings.sound { playSound(FlySynth(built.voices, built.seconds)) }
    DispatchQueue.main.asyncAfter(deadline: .now() + built.seconds) { w.orderOut(nil); Flyover.win = nil; done?() }
  }
}
// What the menu bar and the flyover need from scramble.json (Hangar reads it directly every second; it is a tiny file).
struct ScFile { var on = false, mode = "", target = 0, since = 0.0 }
func readScFile() -> ScFile {
  guard let d = try? Data(contentsOf: URL(fileURLWithPath: MGDIR + "/scramble.json")), let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return ScFile() }
  var f = ScFile()
  let until = (j["until"] as? Double) ?? 0
  f.on = (j["on"] as? Bool ?? false) && (until == 0 || until > Date().timeIntervalSince1970)
  f.mode = j["mode"] as? String ?? ""; if f.mode == "warthog" { f.mode = "cas" }   // the name before the rename; f.target = j["target_agents"] as? Int ?? 0; f.since = j["since"] as? Double ?? 0
  return f
}

struct BroomShape: Shape {   // a broom, 100 x 100
  func path(in r: CGRect) -> Path {
    let s = min(r.width, r.height) / 100, ox = r.midX - 50 * s, oy = r.minY
    func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: ox + x * s, y: oy + y * s) }
    var p = Path()
    p.addLines([pt(80, 4), pt(88, 9), pt(56, 58), pt(48, 53)]); p.closeSubpath()                          // handle
    p.addLines([pt(40, 50), pt(64, 64), pt(60, 70), pt(36, 56)]); p.closeSubpath()                        // binding
    p.addLines([pt(34, 58), pt(60, 73), pt(48, 98), pt(8, 82)]); p.closeSubpath()                         // bristles
    return p
  }
}
struct ScrambleIcon: View {   // Scramble: two jets climbing out together, speed lines behind (Close Air Support: two A-10s)
  var on: Bool
  var a10 = false
  var body: some View {
    ZStack {
      ForEach(0..<2, id: \.self) { i in
        Group {
          if a10 { A10Shape().fill(on ? Color.green : Color.primary.opacity(0.8)) }
          else { JetShape().fill(on ? Color.green : Color.primary.opacity(0.8), style: FillStyle(eoFill: true)) }
        }.frame(width: 20, height: 20).rotationEffect(.degrees(35)).offset(x: CGFloat(i * 12 - 6), y: CGFloat(6 - i * 10))
      }
      ForEach(0..<3, id: \.self) { k in
        Capsule().fill(Color.secondary.opacity(0.5)).frame(width: 8, height: 1.5).rotationEffect(.degrees(35))
          .offset(x: CGFloat(-16 + k * 5), y: CGFloat(14 - k * 3))
      }
    }.frame(width: 40, height: 40)
  }
}
struct Broom: View {
  var body: some View {
    BroomShape().fill(LinearGradient(colors: [Color(red: 0.62, green: 0.42, blue: 0.22), Color(red: 0.86, green: 0.68, blue: 0.32)],
                                     startPoint: .topTrailing, endPoint: .bottomLeading)).frame(width: 38, height: 38)
  }
}
struct Formation: View {   // three jets in a V: Alpha Strike
  var body: some View {
    ZStack {
      ForEach(0..<3) { i in
        Image(systemName: "airplane").font(.system(size: i == 0 ? 15 : 11, weight: .bold)).rotationEffect(.degrees(-90))
          .offset(x: i == 0 ? 0 : (i == 1 ? -11 : 11), y: i == 0 ? -6 : 7)
      }
    }.foregroundStyle(LinearGradient(colors: [.red, .orange], startPoint: .bottom, endPoint: .top)).frame(width: 34, height: 34)
  }
}
// Call-sign icons, all drawn here (no image files), 38 pt like the jet.
struct DropTank: View {   // Jettison: a bomb released and falling away, speed lines behind it
  var body: some View {
    let body = LinearGradient(colors: [Color(red: 0.58, green: 0.45, blue: 0.30), Color(red: 0.86, green: 0.70, blue: 0.42)], startPoint: .top, endPoint: .bottom)
    ZStack {
      ForEach(0..<3) { i in Capsule().fill(Color.secondary.opacity(0.5)).frame(width: CGFloat(10 - i * 2), height: 2.5)
        .offset(x: -13, y: CGFloat(i - 1) * 6) }
      Path { p in   // tail fins (box tail)
        p.addLines([CGPoint(x: 0, y: 6), CGPoint(x: 8, y: 9), CGPoint(x: 8, y: 13), CGPoint(x: 0, y: 16)]); p.closeSubpath()
      }.fill(Color(red: 0.50, green: 0.38, blue: 0.25)).frame(width: 22, height: 22).offset(x: 1, y: 0)
      Ellipse().fill(body).frame(width: 27, height: 14).offset(x: 6)
    }.frame(width: 38, height: 38).scaleEffect(1.15).rotationEffect(.degrees(35))
  }
}
struct GearUp: View {   // Wheels Up: a landing-gear wheel with an up chevron
  var body: some View {
    ZStack {
      Image(systemName: "chevron.up").font(.system(size: 15, weight: .black)).foregroundStyle(.blue).offset(y: -15)
      Circle().stroke(Color.blue, lineWidth: 6).frame(width: 22, height: 22).offset(y: 5)
      Circle().fill(Color.blue.opacity(0.85)).frame(width: 7, height: 7).offset(y: 5)
      Rectangle().fill(Color.blue).frame(width: 3, height: 9).offset(y: -6)
    }.frame(width: 38, height: 38)
  }
}
struct FlightRecorder: View {   // Black Box: the orange flight recorder, carry handle on top, reflective stripes
  var body: some View {
    let orange = Color(red: 1.0, green: 0.48, blue: 0.10)
    ZStack {
      RoundedRectangle(cornerRadius: 3).stroke(orange, lineWidth: 3).frame(width: 14, height: 10).offset(y: -9)
      RoundedRectangle(cornerRadius: 4).fill(orange).frame(width: 30, height: 20).offset(y: 5)
      ForEach(0..<2) { i in Rectangle().fill(Color.white.opacity(0.9)).frame(width: 30, height: 2.5).offset(y: CGFloat(i) * 8 + 1) }
    }.frame(width: 38, height: 38)
  }
}
struct Sym: View {
  let name: String; let color: Color
  var body: some View { Image(systemName: name).font(.system(size: 28, weight: .semibold)).foregroundStyle(color).frame(height: 38) }
}

// ---------------------------------------------------------------- panel
struct Tile<Icon: View>: View {
  let title: String, sub: String, tint: Color, enabled: Bool, glow: Bool, action: () -> Void
  @ViewBuilder let icon: () -> Icon
  var body: some View {
    Button(action: action) {
      VStack(spacing: 5) {
        icon().frame(height: 46)
        Text(title).font(.system(size: 13, weight: .semibold))
        Text(sub).font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.center).frame(minHeight: 26, alignment: .top)
      }
      .frame(maxWidth: .infinity, minHeight: 112).padding(.horizontal, 6)
      .background(RoundedRectangle(cornerRadius: 12).fill(tint.opacity(glow ? 0.22 : 0.10)))
      .overlay(RoundedRectangle(cornerRadius: 12).stroke(tint.opacity(glow ? 0.7 : 0.28), lineWidth: glow ? 1.5 : 1))
      .contentShape(RoundedRectangle(cornerRadius: 12))
    }
    .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.45)
  }
}

let AMBER = Color(red: 1.0, green: 0.72, blue: 0.0)
func scLabel(_ mode: String, _ n: Int) -> String {
  switch mode { case "cap": return "CAP · \(n)"; case "surge": return "Surge · \(n)"; case "cas": return "CAS · \(n)"; case "custom": return "Custom · \(n)"; default: return "Scramble · \(n)" }
}
func scCount(_ mode: String, _ custom: Int) -> Int { switch mode { case "cap": return 12; case "surge": return 25; case "cas": return 30; case "custom": return custom; default: return 20 } }
/// Scramble tile (same footprint as the other tiles): tap = launch / land; the label is a menu: CAP 12, Scramble 20, Surge 25, Close Air Support 30, Custom, Off.
/// Custom shows a - / + stepper (1 to 40). Green glow when on; amber with "disk low" when free disk is under 10 GB.
struct ScrambleTile: View {
  @ObservedObject var m: Model
  let enabled: Bool
  var mode: String { m.st.sc ? m.st.scMode : m.scPick }
  var n: Int { m.st.sc ? m.st.scTarget : scCount(m.scPick, m.customN) }
  var low: Bool { m.st.scDiskLow }
  var tint: Color { low ? AMBER : .green }
  var glow: Bool { m.st.sc }
  var sub: String {
    if low { return "disk low · \(String(format: "%.1f", m.st.scDiskGB ?? 0)) GB free\n" + (m.st.sc ? "ON · \(hm(m.st.scLeft)) left" : "over 20 won't start") }
    return m.st.sc ? "ON · \(hm(m.st.scLeft)) left\n\(n) agents · tap to land" : "off · tap for 8 h\n\(n) agents flying"
  }
  func pick(_ mode: String) { m.scPick = mode; m.setScramble(mode) }
  var body: some View {
    VStack(spacing: 5) {
      ScrambleIcon(on: m.st.sc, a10: mode == "cas").frame(height: 46)
      Menu {
        Button("CAP · 12 agents") { pick("cap") }
        Button("Scramble · 20 agents") { pick("scramble") }
        Button("Surge · 25 agents") { pick("surge") }.disabled(low)
        Button("Close Air Support · 30 agents") { pick("cas") }.disabled(low)
        Button("Custom · \(m.customN) agents") { pick("custom") }.disabled(low && m.customN > 20)
        Divider()
        Button("Off") { if m.st.sc { m.tap(.scramble) } }.disabled(!m.st.sc)
      } label: { Text(scLabel(mode, n) + " ▾").font(.system(size: 13, weight: .semibold)) }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().disabled(!enabled)
      if mode == "custom" {
        HStack(spacing: 6) {
          Button("−") { m.customN = max(1, m.customN - 1) }.buttonStyle(.bordered).controlSize(.mini)
          Text("\(m.customN)").font(.system(size: 11.5, weight: .semibold, design: .monospaced)).frame(minWidth: 22)
          Button("+") { m.customN = min(40, m.customN + 1) }.buttonStyle(.bordered).controlSize(.mini)
          if m.st.sc && m.st.scTarget != m.customN { Button("Set") { m.setScramble("custom") }.buttonStyle(.borderedProminent).controlSize(.mini) }
        }.frame(minHeight: 26)
      } else {
        Text(sub).font(.system(size: 10.5)).foregroundStyle(low ? AMBER : .secondary).lineLimit(2).multilineTextAlignment(.center).frame(minHeight: 26, alignment: .top)
      }
    }
    .frame(maxWidth: .infinity, minHeight: 112).padding(.horizontal, 6)
    .background(RoundedRectangle(cornerRadius: 12).fill(tint.opacity(glow || low ? 0.22 : 0.10)))
    .overlay(RoundedRectangle(cornerRadius: 12).stroke(tint.opacity(glow || low ? 0.7 : 0.28), lineWidth: glow ? 1.5 : 1))
    .contentShape(RoundedRectangle(cornerRadius: 12))
    .onTapGesture { if enabled { m.tap(.scramble) } }
    .opacity(enabled ? 1 : 0.45)
    .help("Tap to launch or land. Click the name to pick CAP 12, Scramble 20, Surge 25, Close Air Support 30 (CAS) or Custom 1-40. Surge, Close Air Support and Custom above 20 need 10 GB free disk.")
  }
}

func ago(_ t: Double?) -> String {
  guard let t = t else { return "never" }
  let d = Date().timeIntervalSince1970 - t
  if d < 90 { return "just now" }
  if d < 3600 { return "\(Int(d / 60)) min ago" }
  if d < 86400 { return "\(Int(d / 3600)) h ago" }
  return "\(Int(d / 86400)) d ago"
}
func hm(_ m: Int) -> String { m >= 60 ? "\(m / 60)h \(m % 60)m" : "\(m) min" }

struct PanelView: View {
  @ObservedObject var m: Model
  var statusLine: String {
    guard m.st.loaded else { return "reading…" }
    var s = "Memory \(m.st.memFree.map { "\($0)%" } ?? "?") free · load \(String(format: "%.1f", m.st.load))"
    if let d = m.st.diskFree { s += " · disk \(String(format: "%.0f", d)) GB" }
    return s
  }
  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      HStack(alignment: .firstTextBaseline) {
        Text("Hangar").font(.system(size: 15, weight: .bold))
        Spacer()
        Text(statusLine).font(.system(size: 11)).monospacedDigit().foregroundStyle(.secondary)
      }
      Picker("", selection: $m.tab) { Text("Cockpit").tag(0); Text("Tower").tag(1); if QUEUE_PATTERN != nil { Text("Queue").tag(2) } }.pickerStyle(.segmented).labelsHidden()
      if m.tab == 2 && QUEUE_PATTERN != nil { queueView } else if m.tab == 1 { lanesView } else if let p = m.pending { confirm(p) } else { tiles }
      if m.busy != nil {
        HStack(spacing: 8) { ProgressView().controlSize(.small); Text(m.busyNote).font(.system(size: 11)).foregroundStyle(.secondary) }
      }
      if !m.result.isEmpty {
        Text(m.result).font(.system(size: 11, weight: .medium)).foregroundStyle(m.result.hasPrefix("Problem") ? Color.red : Color.primary)
          .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
      }
      Divider()
      Text("Afterburner \(m.st.ab ? "ON, \(hm(m.st.abLeft)) left" : "off") · tidy \(ago(m.st.lastTidy)) · save \(ago(m.st.lastSave)) · backup \(ago(m.st.lastBackup))")
        .font(.system(size: 10.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
      HStack {
        Toggle("Open at login", isOn: Binding(get: { m.loginOn }, set: { m.setLogin($0) })).toggleStyle(.switch).controlSize(.mini).font(.system(size: 11))
        Spacer()
        Button("Quit") { NSApp.terminate(nil) }.controlSize(.small)
      }
      HStack {
        Toggle("Animations", isOn: Binding(get: { m.animOn }, set: { m.setAnim($0) })).toggleStyle(.switch).controlSize(.mini).font(.system(size: 11))
        Spacer()
        Toggle("Flyover sound", isOn: Binding(get: { m.flySound }, set: { m.setFlySound($0) })).toggleStyle(.switch).controlSize(.mini).font(.system(size: 11))
      }
    }
    .padding(14).frame(width: 344)
  }
  func laneRow(_ j: LaneJob, running: Bool) -> some View {
    HStack(alignment: .firstTextBaseline, spacing: 6) {
      Circle().fill(running ? (j.alive ? Color.green : Color.red) : (j.held ? Color.gray : Color.orange)).frame(width: 7, height: 7)
      Text((j.priority ? "★ " : "") + j.label).font(.system(size: 11)).lineLimit(2).truncationMode(.tail)
      Spacer(minLength: 4)
      Text(running ? (j.alive ? dur(j.secs) : "dead") : (j.held ? "held \(dur(j.secs))" : "waiting \(dur(j.secs))"))
        .font(.system(size: 10)).monospacedDigit().foregroundStyle(.secondary)
    }
  }
  func lane(_ title: String, _ run: [LaneJob], _ q: [LaneJob], heavy: Bool) -> some View {
    VStack(alignment: .leading, spacing: 4) {
      Text("\(title) · \(run.count) running · \(q.count) queued").font(.system(size: 11.5, weight: .semibold))
      if run.isEmpty && q.isEmpty { Text("idle").font(.system(size: 11)).foregroundStyle(.secondary) }
      ForEach(run) { laneRow($0, running: true) }
      if !q.isEmpty {
        List {
          ForEach(q) { j in laneRow(j, running: false).moveDisabled(j.priority) }
            .onMove { from, to in m.moveQueued(heavy: heavy, from: from, to: to) }
        }
        .listStyle(.plain).scrollContentBackground(.hidden).frame(height: min(CGFloat(q.count) * 24 + 4, 168))
        Text("Drag queued jobs to change the order. ★ priority jobs stay first.").font(.system(size: 9.5)).foregroundStyle(.secondary)
      }
    }
  }
  /// Queue tab: two count tiles, the next three queued jobs, and how fresh the file is (amber when older than 45 min).
  var queueView: some View {
    let q = m.queue
    let age = q.mtime.map { Int(max(0, m.queueTick.timeIntervalSince($0))) }
    let stale = q.found && (age ?? 0) > QUEUE_STALE_SECS
    let tint: Color = stale ? AMBER : .secondary
    func count(_ n: Int, _ label: String, _ c: Color) -> some View {
      VStack(spacing: 2) {
        Text(q.found ? "\(n)" : "–").font(.system(size: 30, weight: .bold, design: .rounded)).monospacedDigit()
        Text(label).font(.system(size: 10.5)).foregroundStyle(.secondary)
      }
      .frame(maxWidth: .infinity, minHeight: 72)
      .background(RoundedRectangle(cornerRadius: 12).fill(c.opacity(0.10)))
      .overlay(RoundedRectangle(cornerRadius: 12).stroke(c.opacity(0.28)))
    }
    return VStack(alignment: .leading, spacing: 8) {
      HStack(spacing: 8) { count(q.running, "running", .green); count(q.queued, "queued", .blue) }
      VStack(alignment: .leading, spacing: 5) {
        Text(q.top.isEmpty ? (q.found ? "Nothing queued" : "No queue file found") : "Next up").font(.system(size: 11.5, weight: .semibold))
        ForEach(q.top) { it in
          HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(it.id).font(.system(size: 10, weight: .semibold, design: .monospaced)).foregroundStyle(.blue).frame(width: 24, alignment: .leading)
            Text(it.text).font(.system(size: 11)).lineLimit(2).truncationMode(.tail).fixedSize(horizontal: false, vertical: true)
          }
        }
      }
      .padding(10).frame(maxWidth: .infinity, alignment: .leading)
      .background(RoundedRectangle(cornerRadius: 12).fill(Color.blue.opacity(0.06)))
      .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.blue.opacity(0.2)))
      if q.found, let t = q.mtime {
        HStack(spacing: 6) {
          Image(systemName: stale ? "exclamationmark.triangle.fill" : "clock").font(.system(size: 10)).foregroundStyle(stale ? AMBER : Color.secondary)
          Text("Updated \(ago(t.timeIntervalSince1970))" + (stale ? " · stale" : "")).lineLimit(1)
            .font(.system(size: 10.5, weight: stale ? .semibold : .regular)).foregroundStyle(stale ? AMBER : Color.secondary)
          Spacer(minLength: 4)
          Text(q.name).font(.system(size: 9.5, design: .monospaced)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
        }
        .padding(.horizontal, 10).padding(.vertical, 6).frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 8).fill(tint.opacity(stale ? 0.18 : 0.06)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(tint.opacity(stale ? 0.6 : 0.15)))
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .onAppear { if ENV["HANGAR_SHOT"] == nil { m.refreshQueue(force: true) } }
  }
  var lanesView: some View {
    VStack(alignment: .leading, spacing: 10) {
      lane("Heavy lane", m.lanes.heavyRun, m.lanes.heavyQ, heavy: true)
      lane("Light lane", m.lanes.lightRun, m.lanes.lightQ, heavy: false)
      if m.lanes.paused > 0 { Text("\(m.lanes.paused) job(s) paused by memguard (memory)").font(.system(size: 10.5)).foregroundStyle(.orange) }
      if !m.lanes.recent.isEmpty {
        Text("Recent").font(.system(size: 11.5, weight: .semibold))
        ForEach(Array(m.lanes.recent.enumerated()), id: \.offset) { _, line in
          Text(line).font(.system(size: 9.5, design: .monospaced)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
        }
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .onAppear { m.refreshLanes() }
  }
  var tiles: some View {
    let idle = m.busy == nil
    return VStack(spacing: 8) {
      HStack(spacing: 8) {
        Tile(title: "Afterburner", sub: m.st.ab ? "ON · \(hm(m.st.abLeft)) left\ntap to land" : "off · tap for 2 h\nmore power", tint: .orange,
             enabled: idle, glow: m.st.ab, action: { m.tap(.afterburner) }) { Jet(on: m.st.ab) }
        ScrambleTile(m: m, enabled: idle)
      }
      HStack(spacing: 8) {
        Tile(title: "Jettison", sub: "old build files\nto the Trash", tint: .brown, enabled: idle, glow: false, action: { m.tap(.tidy) }) { DropTank() }
        Tile(title: "Wheels Up", sub: "save + push\nall code to GitHub", tint: .blue, enabled: idle, glow: false, action: { m.tap(.save) }) {
          GearUp() }
      }
      HStack(spacing: 8) {
        Tile(title: "Black Box", sub: m.st.drive.map { "backup to \($0)" } ?? "no drive", tint: .teal, enabled: idle && m.st.drive != nil, glow: false,
             action: { m.tap(.backup) }) { FlightRecorder() }
          .help(m.st.drive == nil ? (m.st.driveWhy.isEmpty ? "No backup drive attached" : m.st.driveWhy) : "Bundles + files to \(m.st.drive!)")
      }
      Button(action: { m.tap(.all) }) {
        HStack(spacing: 10) {
          Formation()
          VStack(alignment: .leading, spacing: 1) {
            Text("Alpha Strike").font(.system(size: 13, weight: .semibold))
            Text(m.st.drive == nil ? "all at once: jettison → wheels up → black box (no drive)" : "all at once: jettison → wheels up → black box").font(.system(size: 10.5)).foregroundStyle(.secondary)
          }
          Spacer()
        }
        .padding(.horizontal, 12).frame(maxWidth: .infinity, minHeight: 50)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.red.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.red.opacity(0.28)))
        .contentShape(RoundedRectangle(cornerRadius: 12))
      }.buttonStyle(.plain).disabled(!idle).opacity(idle ? 1 : 0.45)
    }
  }
  func confirm(_ p: Pending) -> some View {
    VStack(alignment: .leading, spacing: 8) {
      Text("\(p.act.title): here's the plan. Nothing has changed yet.").font(.system(size: 12, weight: .semibold))
      ScrollView {
        Text(p.plan).font(.system(size: 9.5, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled).padding(6)
      }
      .frame(height: 190)
      .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.05)))
      Text(p.summary).font(.system(size: 11, weight: .medium)).fixedSize(horizontal: false, vertical: true)
      HStack {
        Button("Cancel") { m.pending = nil }.keyboardShortcut(.cancelAction)
        Spacer()
        Button("Confirm") { m.confirm() }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
      }
    }
  }
}

// ---------------------------------------------------------------- app
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
  let model = Model()
  var item: NSStatusItem!
  let pop = NSPopover()
  var timer: Timer?
  func applicationDidFinishLaunching(_ n: Notification) {
    if let dir = ENV["HANGAR_SHOT"] { Shots.run(dir); return }
    let demo = FlyScene(rawValue: ENV["HANGAR_FLYOVER"] ?? "") ?? .dive   // HANGAR_FLYOVER=1 (or cas) means the dive
    if let wav = ENV["HANGAR_FLYOVER_WAV"] {
      let b = Scenes.build(demo, CGSize(width: 1440, height: 900), CALayer(), CACurrentMediaTime()); FlySynth(b.voices, b.seconds).writeWav(wav); NSApp.terminate(nil); return
    }
    if ENV["HANGAR_FLYOVER"] != nil { Flyover.play(demo, force: true) { NSApp.terminate(nil) }; return }   // demo: one scene, then quit
    item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    item.button?.target = self; item.button?.action = #selector(toggle)
    pop.behavior = .transient; pop.delegate = self
    pop.contentViewController = NSHostingController(rootView: PanelView(m: model))
    model.refresh(); updateIcon()
    Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in if !self.pop.isShown { self.model.refresh() } }
    timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.updateIcon() }
    let f0 = readScFile(); lastSince = f0.since; lastKey = f0.on ? f0.mode : ""   // a mode that was already on when Hangar started does not play
    scTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in self.watchScramble() }
  }
  // Scramble watcher (1 s, one tiny file read): shows the A-10 beside the plane while the mode is Close Air Support or the target is 30+, and plays
  // the mode's animation each time a mode is switched on (a new `since` in scramble.json; works for the CLI and for the tile menu):
  // CAP = 2-ship pass, Scramble = 4, Surge = 6, Close Air Support = the A-10 dive. Custom has no animation.
  var scTimer: Timer?
  var lastSince = 0.0, lastKey = "", a10Shown = false, iconKey = ""
  func watchScramble() {
    let f = readScFile()
    let show = f.on && (f.mode == "cas" || f.target >= CAS_MIN_AGENTS)
    if show != a10Shown { a10Shown = show; updateIcon() }
    // plays only on a real switch: off -> a mode, or one mode -> another; renewing the same mode (a new `since`) is silent
    let key = f.on ? f.mode : ""
    if key != lastKey {
      lastKey = key
      let scene: [String: FlyScene] = ["cas": .dive, "cap": .cap, "scramble": .scramble, "surge": .surge]
      if f.on, let sc = scene[f.mode], Date().timeIntervalSince1970 - f.since < 30 { Flyover.play(sc) }
    }
    lastSince = f.since
  }
  func updateIcon() {
    guard let b = item?.button else { return }
    let on = model.st.ab, title = on ? " \(hm(model.st.abLeft))" : ""
    let key = "\(on)|\(a10Shown)"
    if key != iconKey { iconKey = key; b.image = menuBarImage(symbol: on ? "airplane.departure" : "airplane", a10: a10Shown) }
    if b.title != title { b.title = title }
    b.imagePosition = .imageLeading
  }
  // Clicking the plane opens the panel as a small window pinned under the menu bar, inside the visible screen area
  // (the old popover could open half above the top edge on a multi-display setup).
  @objc func toggle() {
    if let w = win, w.isVisible, w.isKeyWindow { w.orderOut(nil); return }
    var anchor: NSRect? = nil
    if let b = item.button, let bw = b.window { anchor = bw.convertToScreen(b.convert(b.bounds, to: nil)) }
    showWindow(anchor)
  }
  func showWindow(_ anchor: NSRect?) {
    model.refresh()
    if win == nil {
      let w = NSWindow(contentViewController: NSHostingController(rootView: PanelView(m: model)))
      w.title = "Hangar"; w.styleMask = [.titled, .closable]; w.isReleasedWhenClosed = false
      win = w
    }
    guard let w = win else { return }
    w.layoutIfNeeded()
    let scr = NSScreen.screens.first(where: { a in anchor.map { a.frame.intersects($0) } ?? false }) ?? NSScreen.main ?? NSScreen.screens[0]
    let vis = scr.visibleFrame, sz = w.frame.size
    if let a = anchor {
      let x = min(max(a.midX - sz.width / 2, vis.minX + 8), vis.maxX - sz.width - 8)
      w.setFrameOrigin(NSPoint(x: x, y: vis.maxY - sz.height - 4))
    } else { w.center() }
    // never above or below the visible area
    var f = w.frame
    if f.maxY > vis.maxY { f.origin.y = vis.maxY - f.height }
    if f.minY < vis.minY { f.origin.y = vis.minY }
    w.setFrame(f, display: true)
    NSApp.activate(ignoringOtherApps: true); w.makeKeyAndOrderFront(nil)
    timer?.invalidate()
    timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in self.model.refresh(); self.updateIcon() }
    lanesTimer?.invalidate()
    lanesTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in if self.win?.isVisible == true { self.model.refreshLanes(); if self.model.tab == 2 { self.model.refreshQueue() } } }
  }
  var lanesTimer: Timer?
  // Opening the app again (Finder/Spotlight) shows the panel centered, in case the menu bar plane is hidden behind the notch.
  var win: NSWindow?
  func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { showWindow(nil); return false }
  func popoverDidClose(_ n: Notification) {
    timer?.invalidate(); timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.updateIcon() }
  }
}

// Screenshot helper: a fake menu bar around the real template image (the flyover frames are filmed from the real thing, see HANGAR_FLYOVER_BACKDROP).
struct MenuBarMock: View {
  let a10: Bool, ab: Bool
  var zoom: CGFloat = 1
  var body: some View {
    let img = menuBarImage(symbol: ab ? "airplane.departure" : "airplane", a10: a10)
    HStack(spacing: 8 * zoom) {
      Spacer(minLength: 0)
      if let i = img {
        Image(nsImage: i).renderingMode(.template).resizable().frame(width: i.size.width * zoom, height: i.size.height * zoom)
      }
      if ab { Text("1h 35m").font(.system(size: 13 * zoom)) }
      Text("Wi-Fi  ·  Tue 12:22 AM").font(.system(size: 13 * zoom)).foregroundStyle(.secondary)
    }
    .foregroundStyle(.primary).padding(.horizontal, 12 * zoom).frame(width: 330 * zoom, height: 24 * zoom)
    .background(Color.primary.opacity(0.07))
  }
}
// ---------------------------------------------------------------- screenshots (HANGAR_SHOT=<dir>): offscreen, never a real status item
enum Shots {
  static var keep = [NSWindow]()
  static func render(_ v: NSView, _ path: String) {
    let b = v.bounds, scale: CGFloat = 2
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(b.width * scale), pixelsHigh: Int(b.height * scale), bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return }
    rep.size = b.size; v.cacheDisplay(in: b, to: rep)
    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
  }
  // Offscreen animation frames (never on the real display): each scene is built into a screen-sized layer tree inside a window parked far off
  // screen, the tree is frozen (speed 0) and stepped with timeOffset, and the presentation layer is rendered into bitmaps.
  // Output per scene: <dir>/anim/<scene>/fNN.png (15 fps, 720 x 450) and <dir>/hangar_anim_<scene>.png (an 8-frame strip).
  static func animFrames(_ dir: String) {
    let scenes: [FlyScene] = [.dive, .afterburner, .jettison, .wheels, .blackbox, .alpha, .cap, .scramble, .surge]
    let size = CGSize(width: 1440, height: 900), fps = 15.0
    func renderScene(_ k: Int) {
      if k >= scenes.count { NSApp.terminate(nil); return }
      let sc = scenes[k], fdir = "\(dir)/anim/\(sc.rawValue)"
      try? FileManager.default.createDirectory(atPath: fdir, withIntermediateDirectories: true)
      let w = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: size.width, height: size.height), styleMask: .borderless, backing: .buffered, defer: false)
      let v = NSView(frame: NSRect(origin: .zero, size: size)); v.wantsLayer = true; w.contentView = v; w.orderFrontRegardless(); keep.append(w)
      let root = v.layer!
      let built = Scenes.build(sc, size, CALayer(), CACurrentMediaTime())   // only for its length
      let n = Int(built.seconds * fps), ow = 720, oh = 450
      var frames = [CGImage]()
      func shoot(_ i: Int) {
        if i >= n {
          // strip of 8 frames with time labels
          let pick = (0..<8).map { min(n - 1, Int(Double($0) / 7 * Double(n - 1))) }, cell = CGSize(width: 480, height: 300)
          let sw = Int(cell.width) * 4 + 5 * 6, sh = Int(cell.height) * 2 + 3 * 6
          if let ctx = CGContext(data: nil, width: sw, height: sh, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
            ctx.setFillColor(CGColor(gray: 0.1, alpha: 1)); ctx.fill(CGRect(x: 0, y: 0, width: sw, height: sh))
            for (j, fi) in pick.enumerated() {
              let col = j % 4, row = j / 4, x = 6 + col * (Int(cell.width) + 6), y = sh - (row + 1) * (Int(cell.height) + 6)
              ctx.draw(frames[fi], in: CGRect(x: x, y: y, width: Int(cell.width), height: Int(cell.height)))
              let label = String(format: "t = %.1f s", Double(fi) / fps)
              NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
              (label as NSString).draw(at: NSPoint(x: x + 8, y: y + 6), withAttributes: [.font: NSFont.monospacedSystemFont(ofSize: 11, weight: .medium), .foregroundColor: NSColor.white])
              NSGraphicsContext.restoreGraphicsState()
            }
            if let im = ctx.makeImage() { let rep = NSBitmapImageRep(cgImage: im); try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/hangar_anim_\(sc.rawValue).png")) }
          }
          w.orderOut(nil); renderScene(k + 1); return
        }
        // fresh scene each frame, frozen at time i/fps; the in-flight values are baked into the model layers, then the model tree is rendered
        CATransaction.begin(); CATransaction.setDisableActions(true)
        for l in root.sublayers ?? [] { l.removeFromSuperlayer() }
        Flyover.addBackdrop(root)
        let scene = CALayer(); scene.frame = root.bounds; root.addSublayer(scene)
        let t0 = CACurrentMediaTime(); _ = Scenes.build(sc, size, scene, t0)
        scene.speed = 0; scene.timeOffset = t0 + Double(i) / fps
        CATransaction.commit(); CATransaction.flush()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.03) {
          func bake(_ l: CALayer) {
            if let p = l.presentation() {
              l.position = p.position; l.opacity = p.opacity; l.bounds = p.bounds; l.transform = p.transform
              // CALayer.render(in:) ignores a scale on a CAShapeLayer, so bake a pure scale into its path (the live window honours the transform)
              if let sh = l as? CAShapeLayer, let path = sh.path, abs(p.transform.m11 - 1) > 0.001, abs(p.transform.m12) < 0.001 {
                let c = CGPoint(x: sh.bounds.midX, y: sh.bounds.midY), k = p.transform.m11
                var t = CGAffineTransform(translationX: c.x, y: c.y).scaledBy(x: k, y: k).translatedBy(x: -c.x, y: -c.y)
                sh.path = path.copy(using: &t); sh.lineWidth *= k; sh.transform = CATransform3DIdentity
              }
            }
            for c in l.sublayers ?? [] { bake(c) }
          }
          func strip(_ l: CALayer) { l.removeAllAnimations(); for c in l.sublayers ?? [] { strip(c) } }
          CATransaction.begin(); CATransaction.setDisableActions(true); bake(scene); strip(scene); scene.speed = 1; scene.timeOffset = 0; CATransaction.commit()
          if let ctx = CGContext(data: nil, width: ow, height: oh, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
            ctx.scaleBy(x: CGFloat(ow) / size.width, y: CGFloat(oh) / size.height)
            root.render(in: ctx)
            if let im = ctx.makeImage() {
              frames.append(im)
              let rep = NSBitmapImageRep(cgImage: im); try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: String(format: "%@/f%03d.png", fdir, i)))
            }
          }
          shoot(i + 1)
        }
      }
      shoot(0)
    }
    renderScene(0)
  }
  static func run(_ dir: String) {
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    let live = Model(); let (_, out) = runTool(TIDY, ["--status"]); if let s = parseStatus(out) { live.st = s }
    let ab = Model(); ab.st = live.st; ab.st.ab = true; ab.st.abLeft = 95; ab.result = "afterburner ON until 4:05 PM (2 h 00 min): Hangar button"
    let conf = Model(); conf.st = live.st
    let (_, plan) = runTool(TIDY, ["--dry-run", "--only", "clean"])
    conf.pending = Pending(act: .tidy, plan: plan.trimmingCharacters(in: .whitespacesAndNewlines), summary: summaryLine(plan))
    let ln = Model(); ln.st = live.st; ln.tab = 1; ln.lanes = readLanes()
    let sc1 = Model(); sc1.st = live.st; sc1.st.sc = true; sc1.st.scLeft = 262; sc1.st.scMode = "surge"; sc1.st.scTarget = 25; sc1.st.scDiskLow = false
    let sc2 = Model(); sc2.st = live.st; sc2.st.sc = false; sc2.scPick = "custom"; sc2.customN = 14; sc2.st.scDiskLow = true; sc2.st.scDiskGB = 4.2
    let sc3 = Model(); sc3.st = live.st; sc3.st.sc = true; sc3.st.scLeft = 478; sc3.st.scMode = "cas"; sc3.st.scTarget = 30; sc3.st.scDiskLow = false
    sc3.result = "scramble ON (Close Air Support) until 8:20 AM (8h 0m): keep 30 agents running"
    let sc4 = Model(); sc4.st = live.st; sc4.st.sc = false; sc4.scPick = "cas"; sc4.st.scDiskLow = true; sc4.st.scDiskGB = 4.2
    let q1 = Model(); q1.st = live.st; q1.tab = 2; q1.queue = readQueue(); q1.queue.mtime = Date().addingTimeInterval(-12 * 60)
    let q2 = Model(); q2.st = live.st; q2.tab = 2; q2.queue = q1.queue; q2.queue.mtime = Date().addingTimeInterval(-62 * 60)
    let cases: [(String, Model)] = [("queue", q1), ("queue_stale", q2), ("panel", live), ("afterburner_on", ab), ("confirm_tidy", conf), ("lanes", ln), ("scramble_surge_on", sc1), ("scramble_custom_disklow", sc2), ("scramble_cas_on", sc3), ("scramble_cas_disklow", sc4)]
    var jobs = [(Model, String, String)]()
    for (n, mdl) in cases { for ap in ["light", "dark"] { jobs.append((mdl, ap, "\(dir)/hangar_\(n)_\(ap).png")) } }
    // menu bar strips (the real template image on a fake bar) and the flyover frame strip
    var extras = [(AnyView, String, String)]()
    for ap in ["light", "dark"] {
      extras.append((AnyView(MenuBarMock(a10: false, ab: false)), ap, "\(dir)/hangar_menubar_plain_\(ap).png"))
      extras.append((AnyView(MenuBarMock(a10: true, ab: false)), ap, "\(dir)/hangar_menubar_a10_\(ap).png"))
      extras.append((AnyView(MenuBarMock(a10: true, ab: true)), ap, "\(dir)/hangar_menubar_a10_afterburner_\(ap).png"))
      extras.append((AnyView(MenuBarMock(a10: true, ab: false, zoom: 5)), ap, "\(dir)/hangar_menubar_a10_zoom_\(ap).png"))
    }
    func extra(_ i: Int) {
      if i >= extras.count { animFrames(dir); return }
      let (v, ap, path) = extras[i]
      let a = NSAppearance(named: ap == "dark" ? .darkAqua : .aqua)
      let host = NSHostingView(rootView: v.background(Color(nsColor: .windowBackgroundColor)))
      host.appearance = a
      let size = host.fittingSize; host.frame = NSRect(origin: .zero, size: size)
      let w = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: size.width, height: size.height), styleMask: [.borderless], backing: .buffered, defer: false)
      w.appearance = a; w.contentView = host; w.orderFrontRegardless(); keep.append(w)
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { render(host, path); w.orderOut(nil); extra(i + 1) }
    }
    func step(_ i: Int) {
      if i >= jobs.count { extra(0); return }
      let (mdl, ap, path) = jobs[i]
      let a = NSAppearance(named: ap == "dark" ? .darkAqua : .aqua)
      let host = NSHostingView(rootView: PanelView(m: mdl).background(Color(nsColor: .windowBackgroundColor)))
      host.appearance = a
      let size = host.fittingSize; host.frame = NSRect(origin: .zero, size: size)
      let w = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: size.width, height: size.height), styleMask: [.borderless], backing: .buffered, defer: false)
      w.appearance = a; w.contentView = host; w.orderFrontRegardless(); keep.append(w)
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { render(host, path); w.orderOut(nil); step(i + 1) }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { step(0) }
  }
}

// Parser check without a window: HANGAR_QUEUE_PARSE=<file> prints running, queued and the first ids as JSON and exits.
if let f = ENV["HANGAR_QUEUE_PARSE"] {
  let r = parseQueue((try? String(contentsOfFile: f, encoding: .utf8)) ?? "")
  let j: [String: Any] = ["running": r.running, "queued": r.queued.count, "ids": r.queued.map { $0.id }, "top": r.queued.prefix(3).map { $0.text }]
  print(String(decoding: (try? JSONSerialization.data(withJSONObject: j)) ?? Data(), as: UTF8.self)); exit(0)
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
