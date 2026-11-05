import AppKit
import SwiftUI
import ServiceManagement
// Hangar: a menu-bar remote for memguard's own commands, with racing names. It runs these commands and shows their result line, nothing else:
//   DRS = afterburner on 2h / off  ·  Box Box = tidy --only clean  ·  Lights Out = tidy --only save  ·  Telemetry = tidy --only backup
//   Pit Stop = tidy (all three)  ·  agent modes (scramble): Safety Car 12, Race Pace 20, Quali Mode 25, Full Send 30, Custom 1-40
// Old names (Afterburner, Jettison, Wheels Up, Black Box, Alpha Strike, CAP, Scramble, Surge, Close Air Support) remain as CLI aliases.
// Box Box, Telemetry and Pit Stop first run `tidy --dry-run ...`, show that plan and wait for one Confirm. No network of its own, no admin
// rights, no screen animations. A short engine blip plays only on a click (switch "Engine sound" off to mute). "Open at login" is OFF until
// you switch it on in the panel (SMAppService). Build: ./build.sh  Screenshots: HANGAR_SHOT=<dir>  Art: HANGAR_ART=<dir>.
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

// ---------------------------------------------------------------- queue tab (Nov 4-5, 2026: the agent queue, read-only)
// Source 1 (preferred): the agentq log (agents.jsonl), which Claude Code hooks append to on every agent start and finish (see agentq.py),
// plus the PENDING backlog (`queue add` / `queue done`). Path: HANGAR_QUEUE_JSONL, else ~/.config/memguard/queue/agents.jsonl.
// Source 2 (fallback): the newest dated markdown file matched by HANGAR_QUEUE_FILE (a path or a glob such as ~/notes/AGENT_QUEUE_*.md):
// rows R<id> under "## Running", Q<id> under "## Queued"/"## PENDING", F<id> under "## Finished"; struck-through rows are skipped.
// Shows running / finished today / pending counts, the next three pending items, and a STALE warning when the newest entry is older than
// HANGAR_QUEUE_STALE_HOURS (2) inside business hours HANGAR_BUSINESS_HOURS ("8-18", local clock). Read-only, no network, re-read at most
// every 60 s while the tab is open. With neither source present there is no Queue tab.
let QUEUE_PATTERN: String? = ENV["HANGAR_QUEUE_FILE"]
let QUEUE_JSONL = ((ENV["HANGAR_QUEUE_JSONL"] ?? (HOMEDIR + "/.config/memguard/queue/agents.jsonl")) as NSString).expandingTildeInPath
var QUEUE_ON: Bool { QUEUE_PATTERN != nil || FileManager.default.fileExists(atPath: QUEUE_JSONL) }
let QUEUE_STALE_SECS = (Double(ENV["HANGAR_QUEUE_STALE_HOURS"] ?? "") ?? 2) * 3600
let QUEUE_LOST_SECS = 12 * 3600.0
let QUEUE_MIN_REREAD = 60.0
let BUSINESS_HOURS: (Int, Int) = {
  let parts = (ENV["HANGAR_BUSINESS_HOURS"] ?? "8-18").split(separator: "-").compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }
  return parts.count == 2 ? (parts[0], parts[1]) : (8, 18)
}()
struct QItem: Identifiable { let id: String; let text: String }
struct QueueInfo { var found = false, name = "", running = 0, finished = 0, queued = 0, top = [QItem](), mtime: Date? = nil, runningTop = [QItem]() }
func queueStale(_ q: QueueInfo, now: Date = Date()) -> Bool {
  guard q.found else { return false }
  let h = Calendar.current.component(.hour, from: now)
  guard h >= BUSINESS_HOURS.0 && h < BUSINESS_HOURS.1 else { return false }
  guard let t = q.mtime else { return true }
  return now.timeIntervalSince(t) > QUEUE_STALE_SECS
}
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
func parseQueue(_ md: String) -> (running: Int, queued: [QItem], finished: Int) {
  var sect = 0, running = 0, finished = 0, queued = [QItem]()
  for raw in md.split(separator: "\n", omittingEmptySubsequences: true) {
    let line = raw.trimmingCharacters(in: .whitespaces)
    if line.hasPrefix("#") {
      let l = line.lowercased()
      sect = !l.hasPrefix("## ") ? 0 : l.contains("running") ? 1 : (l.contains("queued") || l.contains("pending")) && !l.contains("done") ? 2 : l.contains("finished") ? 3 : 0
      continue
    }
    guard sect != 0, line.hasPrefix("|") else { continue }
    let c = queueCells(line)
    guard c.count >= 2, !c[1].hasPrefix("~~") else { continue }   // struck-through rows start with ~~ and fail the id test too
    if sect == 1, isQueueId(c[0], "R") { running += 1 }
    if sect == 2, isQueueId(c[0], "Q") { queued.append(QItem(id: c[0], text: queueClean(c[1]))) }
    if sect == 3, isQueueId(c[0], "F") { finished += 1 }
  }
  return (running, queued, finished)
}
/// Folds the agentq event log (same rules as agentq.py): an agent is running when its latest start is newer than its latest finish and it
/// started less than 12 h ago; tool_use ids alias to agent ids; pending items are pending_add minus pending_done.
func foldQueueLog(_ text: String, now: Date = Date()) -> QueueInfo {
  var evs = [[String: Any]]()
  for raw in text.split(separator: "\n") {
    if let d = raw.data(using: .utf8), let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] { evs.append(j) }
  }
  var alias = [String: String]()
  for e in evs { if let i = e["id"] as? String, let t = e["tuid"] as? String, !t.isEmpty, i != t { alias[t] = i } }
  struct A { var start = Double.infinity, lastStart = 0.0, lastEnd = 0.0, desc = "" }
  var agents = [String: A](), order = [String](), pend = [(String, String)](), done = Set<String>(), newest = 0.0
  for e in evs {
    let ts = (e["ts"] as? Double) ?? 0; newest = max(newest, ts)
    switch e["ev"] as? String ?? "" {
    case "start", "info", "finish":
      guard var k = (e["id"] as? String) ?? (e["tuid"] as? String) else { continue }
      k = alias[k] ?? k
      if agents[k] == nil { agents[k] = A(); order.append(k) }
      var a = agents[k]!
      if a.desc.isEmpty, let d = (e["desc"] as? String) ?? (e["prompt"] as? String) { a.desc = d }
      if (e["ev"] as? String) == "finish" { a.lastEnd = max(a.lastEnd, ts); if a.start == .infinity { a.start = ts } }
      else { a.start = min(a.start, ts); if (e["ev"] as? String) == "start" && e["via"] == nil { a.lastStart = max(a.lastStart, ts) } }   // a PostToolUse row (via: post) can land after the finish
      agents[k] = a
    case "pending_add": if let p = e["pid"] as? String { pend.append((p, e["text"] as? String ?? "")) }
    case "pending_done": if let p = e["pid"] as? String { done.insert(p) }
    default: break
    }
  }
  let cal = Calendar.current, t = now.timeIntervalSince1970
  var q = QueueInfo(); q.found = true
  for k in order {
    let a = agents[k]!
    if a.lastEnd > 0 && a.lastEnd >= a.lastStart {
      if cal.isDate(Date(timeIntervalSince1970: a.lastEnd), inSameDayAs: now) { q.finished += 1 }
    } else if t - a.start <= QUEUE_LOST_SECS {
      q.running += 1; q.runningTop.append(QItem(id: "R\(q.running)", text: a.desc.isEmpty ? k : a.desc))
    }
  }
  let open = pend.filter { !done.contains($0.0) }.map { QItem(id: $0.0, text: $0.1) }
  q.queued = open.count; q.top = Array(open.prefix(3))
  q.mtime = newest > 0 ? Date(timeIntervalSince1970: newest) : nil
  return q
}
func newestQueueFile(_ pattern: String) -> String? {
  let p = (pattern as NSString).expandingTildeInPath
  guard p.contains("*") || p.contains("?") else { return FileManager.default.fileExists(atPath: p) ? p : nil }
  let dir = (p as NSString).deletingLastPathComponent, pat = (p as NSString).lastPathComponent
  let names = ((try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []).filter { fnmatch(pat, $0, 0) == 0 }.sorted()
  return names.last.map { dir + "/" + $0 }   // dated names (AGENT_QUEUE_2026-11-04.md) sort oldest to newest
}
func readQueue() -> QueueInfo {
  if let text = try? String(contentsOfFile: QUEUE_JSONL, encoding: .utf8) {
    var q = foldQueueLog(text); q.name = (QUEUE_JSONL as NSString).lastPathComponent; return q
  }
  guard let pat = QUEUE_PATTERN, let path = newestQueueFile(pat), let text = try? String(contentsOfFile: path, encoding: .utf8) else { return QueueInfo() }
  let r = parseQueue(text)
  let m = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
  return QueueInfo(found: true, name: (path as NSString).lastPathComponent, running: r.running, finished: r.finished, queued: r.queued.count, top: Array(r.queued.prefix(3)), mtime: m)
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
  var title: String { switch self { case .afterburner: return "DRS"; case .scramble: return "Agent mode"; case .tidy: return "Box Box"; case .save: return "Lights Out"
    case .backup: return "Telemetry"; case .all: return "Pit Stop" } }   // racing names (Nov 5, 2026); Pit Stop = all three in one stop
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
  @Published var soundOn = EngineSound.on
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
    guard QUEUE_ON, force || Date().timeIntervalSince(queueRead) >= QUEUE_MIN_REREAD else { return }
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
  /// The car of the mode that is on (or would start), for the click sound.
  var car: Car { st.sc ? carFor(mode: st.scMode, target: st.scTarget) : carFor(mode: scPick, target: scCount(scPick, customN)) }
  /// Every tile and menu click comes through here (never a timer): the engine blip plays here, and only here.
  func click(_ a: Act) { guard busy == nil else { return }; EngineSound.blip(car); tap(a) }
  func pickMode(_ mode: String) { guard busy == nil else { return }; scPick = mode; EngineSound.blip(car); setScramble(mode) }
  func tap(_ a: Act) {
    guard busy == nil else { return }
    switch a {
    case .afterburner:
      run(a, AFTERBURNER, st.ab ? ["off", "turned off in Hangar"] : ["on", "2h", "Hangar button"])
    case .scramble: if st.sc { run(a, SCRAMBLE, ["off", "turned off in Hangar"]) } else { setScramble(scPick) }
    case .save: run(a, TIDY, a.tidyArgs)
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
    case "cas": run(.scramble, SCRAMBLE, ["cas", "8h"] + why)   // Full Send, 30 agents
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
  func setSound(_ on: Bool) { EngineSound.on = on; soundOn = EngineSound.on }
  func setLogin(_ on: Bool) {
    do { if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() } }
    catch { result = "Open at login: \(error.localizedDescription)" }
    loginOn = SMAppService.mainApp.status == .enabled
  }
}

// ---------------------------------------------------------------- car art (original vector drawings, no image files, no marks)
// Two side profiles, front to the right, drawn in design units with y pointing down:
//   .e46  a 2000s German straight-six sports coupe (E46 M3 proportions: flared arches, long hood, short deck, Hofmeister-kink side glass,
//         fender gill, quad exhaust tips). Used for Safety Car 12 and Race Pace 20 (and Custom below 25).
//   .f1   a 2008-2014 V8-era single-seater (stepped nose, wide low front wing, exposed wheels, airbox over the driver, sidepods, tall narrow
//         rear wing). Matte black with an orange accent; no team livery. Used for Quali Mode 25 and Full Send 30 (and Custom 25+).
// No badges, logos, liveries or traced photos. The same code draws the monochrome menu-bar template and the colour art for the window.
// HANGAR_ART=<dir> writes SVG, PDF and 1x/2x PNG copies of both and exits.
enum Car: String { case e46, f1 }
func carFor(mode: String, target: Int) -> Car { mode == "surge" || mode == "cas" || (mode == "custom" && target >= 25) ? .f1 : .e46 }
extension Car {
  var box: CGRect { self == .e46 ? CGRect(x: 0, y: 2, width: 240, height: 78) : CGRect(x: 0, y: 31, width: 240, height: 49) }
  var label: String { self == .e46 ? "sports coupe" : "single-seater" }
}
let ORANGE = CGColor(red: 1.0, green: 0.45, blue: 0.0, alpha: 1)
func P(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x, y: y) }
func poly(_ pts: [(CGFloat, CGFloat)]) -> CGPath { let p = CGMutablePath(); p.addLines(between: pts.map { P($0.0, $0.1) }); p.closeSubpath(); return p }
func circle(_ c: CGPoint, _ r: CGFloat) -> CGPath { CGPath(ellipseIn: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r), transform: nil) }
func rrect(_ r: CGRect, _ rad: CGFloat) -> CGPath { CGPath(roundedRect: r, cornerWidth: rad, cornerHeight: rad, transform: nil) }

// --- the coupe
let E46_REAR = P(51, 61), E46_FRONT = P(192, 61), E46_TYRE: CGFloat = 17, E46_ARCH: CGFloat = 22
func e46Body() -> CGPath {
  let p = CGMutablePath()
  p.move(to: P(9, 70))
  p.addCurve(to: P(5, 58), control1: P(6, 67), control2: P(5, 63))                 // rear bumper
  p.addLine(to: P(5.5, 49))
  p.addCurve(to: P(9, 32.5), control1: P(5.5, 42), control2: P(6, 36))             // tail, up to the deck
  p.addLine(to: P(12.5, 30))                                                        // small boot-lid lip
  p.addCurve(to: P(41, 28), control1: P(20, 29.4), control2: P(32, 28.5))           // short deck
  p.addCurve(to: P(86, 9.2), control1: P(52, 25), control2: P(68, 12))              // C-pillar, coupe slope
  p.addCurve(to: P(128, 9), control1: P(100, 7.2), control2: P(116, 7.4))           // roof
  p.addCurve(to: P(163, 27.5), control1: P(140, 13), control2: P(152, 21))          // windscreen
  p.addCurve(to: P(223, 35.8), control1: P(178, 28.2), control2: P(200, 31.2))      // long hood with a power dome
  p.addCurve(to: P(235.5, 45), control1: P(230, 37.2), control2: P(234, 40))        // nose
  p.addCurve(to: P(236, 58), control1: P(236.8, 49), control2: P(236.8, 54))        // front bumper
  p.addCurve(to: P(231, 70.5), control1: P(235.5, 63), control2: P(235, 68))        // splitter
  let a = asin(9.5 / E46_ARCH)
  p.addLine(to: P(E46_FRONT.x + E46_ARCH * cos(a), 70.5))
  p.addArc(center: E46_FRONT, radius: E46_ARCH, startAngle: a, endAngle: .pi - a, clockwise: true)    // front arch, over the top
  p.addLine(to: P(E46_FRONT.x - E46_ARCH * cos(a) - 2, 71.5))                      // side skirt
  p.addLine(to: P(E46_REAR.x + E46_ARCH * cos(a) + 2, 71.5))
  p.addLine(to: P(E46_REAR.x + E46_ARCH * cos(a), 70.5))
  p.addArc(center: E46_REAR, radius: E46_ARCH, startAngle: a, endAngle: .pi - a, clockwise: true)      // rear arch
  p.closeSubpath()
  return p
}
func e46Glass() -> CGPath {   // the side glass, with the forward hook at the base of the C-pillar
  let p = CGMutablePath()
  p.move(to: P(63.5, 27.6))
  p.addLine(to: P(153.5, 28.2))
  p.addCurve(to: P(127, 12), control1: P(146, 22), control2: P(138, 15.5))
  p.addCurve(to: P(87, 12.2), control1: P(114, 10.6), control2: P(100, 10.6))
  p.addCurve(to: P(60.5, 25.6), control1: P(76, 14), control2: P(66, 19.5))
  p.addQuadCurve(to: P(63.5, 27.6), control: P(59.4, 27.8))
  p.closeSubpath()
  return p
}
let e46Pillar = poly([(95.6, 11.3), (97.6, 11.1), (101.4, 28), (99.4, 28)])            // frameless door / quarter glass split
let e46Mirror = poly([(150, 28.4), (151.6, 23.8), (157.6, 23.4), (158.6, 26.6), (155, 28.6)])
let e46Gill = poly([(164.5, 45.6), (174, 43.4), (174.2, 45.2), (164.7, 47.4)])           // the fender vent behind the front wheel
let e46Exhaust = [rrect(CGRect(x: 2.4, y: 68.6, width: 10, height: 2.7), 1.3), rrect(CGRect(x: 3.2, y: 71.7, width: 9.2, height: 2.6), 1.3)]   // quad tips (one pair a side)

// --- the single-seater
let F1_REAR = P(42, 62.7), F1_FRONT = P(190, 62.7), F1_TYRE: CGFloat = 15.3, F1_RIM: CGFloat = 7.6
func f1Body() -> CGPath {   // nose, monocoque, airbox, engine cover, gearbox
  let p = CGMutablePath()
  p.move(to: P(231, 63.5))
  p.addCurve(to: P(186, 54.5), control1: P(222, 61), control2: P(205, 57))          // nose top
  p.addLine(to: P(180, 54)); p.addLine(to: P(178.5, 50.6))                          // the step
  p.addCurve(to: P(149, 47.6), control1: P(168, 49.4), control2: P(156, 48.4))      // chassis top
  p.addLine(to: P(129, 47.2))                                                       // cockpit rim
  p.addLine(to: P(128.4, 40))
  p.addCurve(to: P(122, 34), control1: P(128.4, 36), control2: P(126.5, 34.2))      // airbox over the driver
  p.addCurve(to: P(70, 46.5), control1: P(108, 34.5), control2: P(90, 39))          // engine cover
  p.addCurve(to: P(32, 57), control1: P(58, 51), control2: P(46, 54.5))
  p.addLine(to: P(20, 59)); p.addLine(to: P(19.5, 63.5)); p.addLine(to: P(32, 64.5))   // gearbox and crash structure
  p.addLine(to: P(50, 68)); p.addLine(to: P(52, 72)); p.addLine(to: P(180, 72))
  p.addCurve(to: P(218, 66.5), control1: P(190, 70.5), control2: P(205, 67.5))      // raised chassis underside
  p.addCurve(to: P(231, 63.5), control1: P(226, 66), control2: P(230.5, 65.5))
  p.closeSubpath()
  return p
}
func f1Sidepod() -> CGPath {
  let p = CGMutablePath()
  p.move(to: P(160, 56.4))
  p.addCurve(to: P(90, 58), control1: P(140, 54.4), control2: P(110, 55))
  p.addCurve(to: P(62, 66.5), control1: P(78, 60), control2: P(70, 64))            // coke-bottle taper
  p.addLine(to: P(60, 72)); p.addLine(to: P(162.5, 72)); p.addLine(to: P(162.5, 58.6))
  p.addQuadCurve(to: P(160, 56.4), control: P(162.5, 56.5))
  p.closeSubpath()
  return p
}
let f1Inlet = rrect(CGRect(x: 155.5, y: 57.8, width: 5.2, height: 8.6), 1.6)
let f1Floor = [poly([(46, 71), (184, 71), (184, 74.6), (46, 74.6)]), poly([(24, 66.5), (47, 71), (47, 74.6), (26, 70.6)])]   // plank + diffuser
let f1FrontWing = [poly([(211.5, 76), (238.6, 76), (238.6, 70.2), (236, 67.2), (226, 67), (214, 69.6), (211.5, 72.6)]),        // endplate
                   poly([(219.4, 65.8), (222.8, 65.8), (225, 70), (221.6, 70)])]                                                  // pylon
let f1RearWing = [poly([(7.6, 34.6), (28.6, 34.6), (28.6, 37.2), (27.2, 58), (21, 61), (9, 60.6), (7.6, 40)]),                    // endplate
                  poly([(25.5, 52.2), (36, 50.2), (36, 55.2), (25.5, 57.2)])]                                                     // pillar
let f1Arms: [(CGPoint, CGPoint)] = [(P(172, 55.5), P(190, 59.8)), (P(174, 66.2), P(190, 66.6)), (P(176, 66), P(188, 58.6)),      // front
                                     (P(66, 58.2), P(42, 59.6)), (P(66, 67), P(42, 66.2))]                                       // rear
let f1Helmet = P(137.5, 43.6)
let f1Mirror = poly([(151, 45.4), (157.6, 44.8), (158, 47.6), (151.6, 48.2)])
let f1AirboxMouth = rrect(CGRect(x: 125.6, y: 36.6, width: 3.2, height: 6.4), 1.2)

// --- drawing
func carTransform(_ k: Car, _ rect: CGRect) -> CGAffineTransform {
  let b = k.box, s = min(rect.width / b.width, rect.height / b.height)
  let ox = rect.midX - b.midX * s, oy = rect.midY - b.midY * s
  return CGAffineTransform(a: s, b: 0, c: 0, d: s, tx: ox, ty: oy)
}
func strokeArm(_ a: CGPoint, _ b: CGPoint, _ w: CGFloat) -> CGPath {
  let p = CGMutablePath(); p.move(to: a); p.addLine(to: b)
  return p.copy(strokingWithWidth: w, lineCap: .round, lineJoin: .round, miterLimit: 4)
}
/// Draws one car into a y-down context. template = one solid colour with see-through glass and wheel gaps (for the menu bar).
func drawCar(_ k: Car, _ ctx: CGContext, _ rect: CGRect, template: Bool) {
  ctx.saveGState()
  ctx.concatenate(carTransform(k, rect))
  ctx.beginTransparencyLayer(auxiliaryInfo: nil)   // clears below only cut this layer, never what is behind the image
  let ink = CGColor(gray: 0, alpha: 1)
  func fill(_ p: CGPath, _ c: CGColor) { ctx.addPath(p); ctx.setFillColor(c); ctx.fillPath() }
  func clear(_ p: CGPath) { ctx.saveGState(); ctx.setBlendMode(.clear); ctx.addPath(p); ctx.fillPath(); ctx.restoreGState() }
  func grad(_ p: CGPath, _ cs: [CGColor], _ a: CGPoint, _ b: CGPoint) {
    ctx.saveGState(); ctx.addPath(p); ctx.clip()
    if let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: cs as CFArray, locations: nil) { ctx.drawLinearGradient(g, start: a, end: b, options: [.drawsBeforeStartLocation, .drawsAfterEndLocation]) }
    ctx.restoreGState()
  }
  func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(red: r, green: g, blue: b, alpha: a) }
  func wheel(_ c: CGPoint, _ r: CGFloat, _ rim: CGFloat, spokes: Int, gap: CGFloat) {
    if template {
      clear(circle(c, r + gap)); fill(circle(c, r), ink); clear(circle(c, rim)); fill(circle(c, rim * 0.62), ink)
      return
    }
    fill(circle(c, r), rgb(0.07, 0.07, 0.08))
    ctx.addPath(circle(c, r - 1.2)); ctx.setStrokeColor(rgb(0.2, 0.2, 0.22)); ctx.setLineWidth(0.8); ctx.strokePath()   // sidewall
    grad(circle(c, rim), [rgb(0.86, 0.87, 0.9), rgb(0.48, 0.5, 0.54)], P(c.x, c.y - rim), P(c.x, c.y + rim))
    fill(circle(c, rim * 0.86), rgb(0.16, 0.16, 0.18))
    for i in 0..<spokes {   // spokes in pairs
      let base = CGFloat(i) * 2 * .pi / CGFloat(spokes) - .pi / 2
      for d in [-0.11, 0.11] as [CGFloat] {
        let a = base + d
        fill(strokeArm(P(c.x + cos(a) * rim * 0.2, c.y + sin(a) * rim * 0.2), P(c.x + cos(a) * rim * 0.86, c.y + sin(a) * rim * 0.86), rim * 0.15), rgb(0.78, 0.8, 0.83))
      }
    }
    fill(circle(c, rim * 0.22), rgb(0.62, 0.64, 0.68))
  }
  switch k {
  case .e46:
    if template {
      fill(e46Body(), ink); clear(e46Glass()); fill(e46Pillar, ink); fill(e46Mirror, ink); clear(e46Gill)
      for p in e46Exhaust { fill(p, ink) }
    } else {
      ctx.saveGState(); ctx.setFillColor(rgb(0, 0, 0, 0.22)); ctx.addEllipse(in: CGRect(x: 6, y: 74.5, width: 230, height: 6)); ctx.fillPath(); ctx.restoreGState()   // ground shadow
      for p in e46Exhaust { grad(p, [rgb(0.85, 0.86, 0.88), rgb(0.38, 0.39, 0.42)], P(0, p.boundingBox.minY), P(0, p.boundingBox.maxY)) }
      let body = e46Body()
      for c in [E46_FRONT, E46_REAR] {   // wheel wells
        ctx.saveGState(); ctx.clip(to: CGRect(x: 0, y: 0, width: 240, height: 71.5))
        grad(circle(c, E46_ARCH - 0.3), [rgb(0.02, 0.02, 0.03), rgb(0.1, 0.1, 0.12)], P(0, c.y - E46_ARCH), P(0, c.y + 8)); ctx.restoreGState()
      }
      grad(body, [rgb(0.42, 0.66, 0.9), rgb(0.2, 0.44, 0.75), rgb(0.1, 0.27, 0.52)], P(0, 8), P(0, 72))   // a bright mid blue
      grad(poly([(4, 62), (237, 62), (237, 72), (4, 72)]), [rgb(0.05, 0.12, 0.25, 0), rgb(0.03, 0.07, 0.15, 0.55)], P(0, 60), P(0, 72))   // lower shade
      ctx.saveGState(); ctx.addPath(body); ctx.clip()
      let shoulder = CGMutablePath(); shoulder.move(to: P(8, 37.5)); shoulder.addCurve(to: P(226, 40.5), control1: P(70, 35.6), control2: P(170, 37.6))
      ctx.addPath(shoulder); ctx.setStrokeColor(rgb(0.75, 0.88, 1, 0.75)); ctx.setLineWidth(0.9); ctx.strokePath()   // shoulder crease highlight
      let sill = CGMutablePath(); sill.move(to: P(74, 66.6)); sill.addLine(to: P(169, 66.6))
      ctx.addPath(sill); ctx.setStrokeColor(rgb(0.02, 0.06, 0.14, 0.6)); ctx.setLineWidth(1.2); ctx.strokePath()
      let door = CGMutablePath(); door.move(to: P(101.6, 29)); door.addCurve(to: P(103, 66), control1: P(103.4, 42), control2: P(103.6, 55))
      door.move(to: P(156, 30)); door.addCurve(to: P(160.5, 62), control1: P(158.4, 42), control2: P(160.4, 52))
      ctx.addPath(door); ctx.setStrokeColor(rgb(0.03, 0.1, 0.22, 0.55)); ctx.setLineWidth(0.6); ctx.strokePath()   // shut lines
      ctx.restoreGState()
      for (c, rr) in [(E46_FRONT, E46_ARCH), (E46_REAR, E46_ARCH)] {   // flared arch lips
        let lip = CGMutablePath(); lip.addArc(center: c, radius: rr - 0.6, startAngle: .pi + 0.35, endAngle: -0.35, clockwise: false)
        ctx.addPath(lip); ctx.setStrokeColor(rgb(0.55, 0.78, 1, 0.65)); ctx.setLineWidth(1.0); ctx.strokePath()
      }
      ctx.addPath(e46Glass()); ctx.setStrokeColor(rgb(0.04, 0.05, 0.06)); ctx.setLineWidth(2.2); ctx.setLineJoin(.round); ctx.strokePath()   // shadow-line trim
      grad(e46Glass(), [rgb(0.42, 0.5, 0.58), rgb(0.12, 0.15, 0.19), rgb(0.07, 0.08, 0.1)], P(0, 10), P(0, 28))
      grad(poly([(108, 11.4), (122, 11.4), (98, 27.4), (86, 27.4)]), [rgb(1, 1, 1, 0.16), rgb(1, 1, 1, 0.02)], P(0, 11), P(0, 28))   // reflection
      fill(e46Pillar, rgb(0.06, 0.07, 0.08))
      grad(e46Mirror, [rgb(0.36, 0.6, 0.86), rgb(0.14, 0.34, 0.62)], P(0, 23), P(0, 29))
      grad(e46Gill, [rgb(0.9, 0.92, 0.95), rgb(0.45, 0.47, 0.5)], P(0, 43), P(0, 47.5))
      ctx.saveGState(); ctx.addPath(body); ctx.clip()
      grad(poly([(4, 33.4), (14, 31.2), (16.5, 38.8), (4, 41.2)]), [rgb(0.95, 0.2, 0.18), rgb(0.55, 0.05, 0.05)], P(0, 31), P(0, 41))   // tail lamp
      grad(poly([(221, 37.4), (231.5, 39.6), (234, 43.6), (223, 43.2)]), [rgb(0.97, 0.98, 1), rgb(0.66, 0.7, 0.76)], P(0, 37), P(0, 44))   // headlamp
      fill(poly([(225.5, 57), (237, 56.6), (237, 63.4), (226.5, 63.6)]), rgb(0.05, 0.06, 0.07, 0.9))   // lower intake
      fill(poly([(214, 69.2), (231.6, 69.6), (230.6, 71.2), (214, 71.4)]), rgb(0.05, 0.06, 0.07, 0.8))   // splitter
      fill(poly([(4, 67.4), (31, 67.6), (31, 70.4), (4, 70.2)]), rgb(0.05, 0.06, 0.07, 0.75))       // rear valance
      ctx.restoreGState()
    }
    wheel(E46_FRONT, E46_TYRE, 11.6, spokes: 5, gap: 1.6)
    wheel(E46_REAR, E46_TYRE, 11.6, spokes: 5, gap: 1.6)
  case .f1:
    let body = f1Body(), pod = f1Sidepod()
    if template {
      fill(body, ink); fill(pod, ink); for p in f1Floor { fill(p, ink) }; for p in f1FrontWing { fill(p, ink) }; for p in f1RearWing { fill(p, ink) }
      for (a, b) in f1Arms { fill(strokeArm(a, b, 2.2), ink) }
      fill(f1Mirror, ink); clear(f1Inlet); clear(f1AirboxMouth)
      clear(circle(f1Helmet, 6.6)); fill(circle(f1Helmet, 5.2), ink)
    } else {
      ctx.saveGState(); ctx.setFillColor(rgb(0, 0, 0, 0.25)); ctx.addEllipse(in: CGRect(x: 8, y: 75.4, width: 230, height: 4.6)); ctx.fillPath(); ctx.restoreGState()
      let black = [rgb(0.25, 0.25, 0.27), rgb(0.12, 0.12, 0.13), rgb(0.06, 0.06, 0.07)]
      for p in f1Floor { fill(p, rgb(0.05, 0.05, 0.05)) }
      for (a, b) in f1Arms { fill(strokeArm(a, b, 1.8), rgb(0.1, 0.1, 0.11)) }
      grad(f1RearWing[1], black, P(0, 50), P(0, 57))
      grad(pod, black, P(0, 55), P(0, 72))
      grad(body, black, P(0, 34), P(0, 72))
      ctx.saveGState(); ctx.addPath(body); ctx.addPath(pod); ctx.clip()
      let stripe = CGMutablePath(); stripe.move(to: P(122, 35.6)); stripe.addCurve(to: P(34, 58), control1: P(100, 37), control2: P(60, 49))
      ctx.addPath(stripe); ctx.setStrokeColor(ORANGE); ctx.setLineWidth(1.6); ctx.strokePath()                       // accent along the engine cover
      let side = CGMutablePath(); side.move(to: P(229, 64.2)); side.addCurve(to: P(150, 52), control1: P(205, 59.5), control2: P(175, 53.2))
      ctx.addPath(side); ctx.setStrokeColor(ORANGE); ctx.setLineWidth(1.2); ctx.strokePath()                        // nose line
      let hl = CGMutablePath(); hl.move(to: P(162, 57.6)); hl.addCurve(to: P(92, 59.4), control1: P(140, 56), control2: P(112, 56.4))
      ctx.addPath(hl); ctx.setStrokeColor(rgb(1, 1, 1, 0.14)); ctx.setLineWidth(0.8); ctx.strokePath()             // sidepod highlight
      ctx.restoreGState()
      fill(f1Inlet, rgb(0.01, 0.01, 0.01)); fill(f1AirboxMouth, rgb(0.01, 0.01, 0.01))
      grad(f1FrontWing[0], [rgb(0.2, 0.2, 0.21), rgb(0.07, 0.07, 0.08)], P(0, 67), P(0, 76))
      fill(poly([(212, 74.4), (238.4, 74.4), (238.4, 76), (212, 76)]), ORANGE)                                   // endplate foot
      fill(f1FrontWing[1], rgb(0.08, 0.08, 0.09))
      grad(f1RearWing[0], [rgb(0.22, 0.22, 0.24), rgb(0.07, 0.07, 0.08)], P(0, 35), P(0, 61))
      fill(poly([(7.8, 34.6), (28.6, 34.6), (28.6, 37.2), (7.8, 37.6)]), ORANGE)                                 // wing tip accent
      let el = CGMutablePath(); el.move(to: P(9.6, 40.2)); el.addLine(to: P(27.6, 39.2)); el.move(to: P(9.8, 44.6)); el.addLine(to: P(27.4, 43.4))
      ctx.addPath(el); ctx.setStrokeColor(rgb(0.45, 0.45, 0.48)); ctx.setLineWidth(0.7); ctx.strokePath()          // main plane + flap
      fill(f1Mirror, rgb(0.12, 0.12, 0.13))
      grad(circle(f1Helmet, 5.2), [rgb(1, 0.6, 0.15), rgb(0.85, 0.3, 0)], P(0, 38.4), P(0, 48.8))                  // helmet
      fill(poly([(137.8, 41.6), (142.8, 41.8), (142.6, 44), (137.8, 43.8)]), rgb(0.05, 0.05, 0.08))               // visor
    }
    wheel(F1_FRONT, F1_TYRE, F1_RIM, spokes: 6, gap: 1.8)
    wheel(F1_REAR, F1_TYRE, F1_RIM, spokes: 6, gap: 1.8)
  }
  ctx.endTransparencyLayer()
  ctx.restoreGState()
}
/// A resolution-independent NSImage of a car (redrawn at whatever scale the menu bar or window needs).
func carImage(_ k: Car, height: CGFloat, template: Bool) -> NSImage {
  let w = (height * k.box.width / k.box.height).rounded()
  let img = NSImage(size: NSSize(width: w, height: height), flipped: true) { r in
    if let ctx = NSGraphicsContext.current?.cgContext { drawCar(k, ctx, r, template: template) }
    return true
  }
  img.isTemplate = template; img.accessibilityDescription = "Hangar (\(k.label))"
  return img
}
/// The menu-bar image: the car for the current mode, one monochrome template image (follows light and dark menu bars).
func menuBarImage(_ k: Car) -> NSImage { carImage(k, height: k == .e46 ? 14 : 11, template: true) }

// SVG / PDF / PNG export of the art (HANGAR_ART=<dir>): the SVG is generated from the same paths, so the files never drift from the app.
func svgPathData(_ p: CGPath) -> String {
  var s = ""
  func f(_ v: CGFloat) -> String { String(format: "%.2f", Double(v)) }
  p.applyWithBlock { e in
    let pts = e.pointee.points
    switch e.pointee.type {
    case .moveToPoint: s += "M\(f(pts[0].x)) \(f(pts[0].y))"
    case .addLineToPoint: s += "L\(f(pts[0].x)) \(f(pts[0].y))"
    case .addQuadCurveToPoint: s += "Q\(f(pts[0].x)) \(f(pts[0].y)) \(f(pts[1].x)) \(f(pts[1].y))"
    case .addCurveToPoint: s += "C\(f(pts[0].x)) \(f(pts[0].y)) \(f(pts[1].x)) \(f(pts[1].y)) \(f(pts[2].x)) \(f(pts[2].y))"
    case .closeSubpath: s += "Z"
    @unknown default: break
    }
  }
  return s
}
func carSVG(_ k: Car) -> String {   // the template silhouette as SVG (cut-outs via a mask)
  let b = k.box
  var solid = [CGPath](), cut = [CGPath](), top = [CGPath]()
  func wheel(_ c: CGPoint, _ r: CGFloat, _ rim: CGFloat, _ gap: CGFloat) { cut.append(circle(c, r + gap)); top.append(circle(c, r)); top.append(circle(c, rim * 0.62)) }
  switch k {
  case .e46:
    solid = [e46Body(), e46Pillar, e46Mirror] + e46Exhaust; cut = [e46Glass(), e46Gill]
    wheel(E46_FRONT, E46_TYRE, 11.6, 1.6); wheel(E46_REAR, E46_TYRE, 11.6, 1.6)
  case .f1:
    solid = [f1Body(), f1Sidepod(), f1Mirror] + f1Floor + f1FrontWing + f1RearWing + f1Arms.map { strokeArm($0.0, $0.1, 2.2) }
    cut = [f1Inlet, f1AirboxMouth, circle(f1Helmet, 6.6)]; top = [circle(f1Helmet, 5.2)]
    wheel(F1_FRONT, F1_TYRE, F1_RIM, 1.8); wheel(F1_REAR, F1_TYRE, F1_RIM, 1.8)
  }
  let rims = [k == .e46 ? (E46_FRONT, CGFloat(11.6)) : (F1_FRONT, F1_RIM), k == .e46 ? (E46_REAR, CGFloat(11.6)) : (F1_REAR, F1_RIM)]
  let vb = "\(Int(b.minX)) \(Int(b.minY)) \(Int(b.width)) \(Int(b.height))"
  var o = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"\(vb)\">\n<!-- Hangar \(k.label): original vector art, generated by Hangar.swift (HANGAR_ART). No marks or liveries. -->\n"
  o += "<defs><mask id=\"m\" maskUnits=\"userSpaceOnUse\" x=\"\(b.minX)\" y=\"\(b.minY)\" width=\"\(b.width)\" height=\"\(b.height)\"><rect x=\"\(b.minX)\" y=\"\(b.minY)\" width=\"\(b.width)\" height=\"\(b.height)\" fill=\"white\"/>"
  for p in cut { o += "<path d=\"\(svgPathData(p))\" fill=\"black\"/>" }
  o += "</mask></defs>\n<g fill=\"currentColor\"><g mask=\"url(#m)\">"
  for p in solid { o += "<path d=\"\(svgPathData(p))\"/>" }
  o += "</g>"
  var tyres = [CGPath]()
  for (c, rim) in rims { tyres.append(circle(c, k == .e46 ? E46_TYRE : F1_TYRE)); _ = rim }
  o += "<g mask=\"url(#r)\">" + tyres.map { "<path d=\"\(svgPathData($0))\"/>" }.joined() + "</g>"
  for (c, rim) in rims { o += "<path d=\"\(svgPathData(circle(c, rim * 0.62)))\"/>" }
  if k == .f1 { o += "<path d=\"\(svgPathData(circle(f1Helmet, 5.2)))\"/>" }
  o += "</g>\n<defs><mask id=\"r\" maskUnits=\"userSpaceOnUse\" x=\"\(b.minX)\" y=\"\(b.minY)\" width=\"\(b.width)\" height=\"\(b.height)\"><rect x=\"\(b.minX)\" y=\"\(b.minY)\" width=\"\(b.width)\" height=\"\(b.height)\" fill=\"white\"/>"
  for (c, rim) in rims { o += "<path d=\"\(svgPathData(circle(c, rim)))\" fill=\"black\"/>" }
  o += "</mask></defs>\n</svg>\n"
  return o
}
func writePNG(_ k: Car, _ path: String, height: CGFloat, scale: CGFloat, template: Bool, background: CGColor? = nil) {
  let w = Int((height * k.box.width / k.box.height).rounded() * scale), h = Int(height * scale)
  guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
  if let bg = background { ctx.setFillColor(bg); ctx.fill(CGRect(x: 0, y: 0, width: w, height: h)) }
  ctx.translateBy(x: 0, y: CGFloat(h)); ctx.scaleBy(x: 1, y: -1)   // y down, like the app
  drawCar(k, ctx, CGRect(x: 0, y: 0, width: w, height: h), template: template)
  if let im = ctx.makeImage() { try? NSBitmapImageRep(cgImage: im).representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path)) }
}
func writePDF(_ k: Car, _ path: String, template: Bool) {
  var box = CGRect(x: 0, y: 0, width: k.box.width * 2, height: k.box.height * 2)
  guard let ctx = CGContext(URL(fileURLWithPath: path) as CFURL, mediaBox: &box, nil) else { return }
  ctx.beginPDFPage(nil); ctx.translateBy(x: 0, y: box.height); ctx.scaleBy(x: 1, y: -1)
  drawCar(k, ctx, box, template: template); ctx.endPDFPage(); ctx.closePDF()
}
func exportArt(_ dir: String) {
  try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
  for k in [Car.e46, .f1] {
    try? carSVG(k).write(toFile: "\(dir)/car_\(k.rawValue).svg", atomically: true, encoding: .utf8)
    writePDF(k, "\(dir)/car_\(k.rawValue)_template.pdf", template: true); writePDF(k, "\(dir)/car_\(k.rawValue)_colour.pdf", template: false)
    for s in [1, 2] as [CGFloat] {
      writePNG(k, "\(dir)/car_\(k.rawValue)_menubar@\(Int(s))x.png", height: k == .e46 ? 14 : 11, scale: s, template: true)
      writePNG(k, "\(dir)/car_\(k.rawValue)_colour@\(Int(s))x.png", height: 80, scale: s, template: false)
    }
  }
}

// ---------------------------------------------------------------- engine sound (click only, 2 s or less, synthesised here)
// A short blip of the current car, played only from a click on a Hangar action (never from a timer, a state change or agent work).
// The "Engine sound" switch mutes it (HANGAR_SOUND=off too); the system volume and mute still rule; at most one blip per 2 s.
//   .e46  straight six, 4-stroke (3 firings per crank turn), idle ~950 rpm, rev to ~7,000 and back; uneven cylinder pulses give the
//         rasp, two exhaust resonances give the body, band-passed noise over the firing rhythm gives the intake howl; overrun crackle.
//   .f1   2.4 L V8, flat-plane (4 firings per turn), ~5,000 rpm to ~18,000 and back; higher pipe resonances, a hard scream.
// HANGAR_ENGINE_WAV=<file> HANGAR_ENGINE=e46|f1 writes the blip to a WAV and exits (tests; nothing plays).
enum EngineSound {
  static var on: Bool {
    get { if let e = ENV["HANGAR_SOUND"], ["off", "0", "no"].contains(e.lowercased()) { return false }
          return (UserDefaults.standard.object(forKey: "engineSound") as? Bool) ?? true }
    set { UserDefaults.standard.set(newValue, forKey: "engineSound") }
  }
  static let rate = 44100.0
  static func samples(_ k: Car) -> [Float] {
    let v8 = k == .f1
    let secs = v8 ? 1.7 : 1.8, n = Int(secs * rate)
    let idle = v8 ? 5000.0 : 950.0, peak = v8 ? 18000.0 : 7000.0, perRev = v8 ? 4.0 : 3.0
    let tUp0 = 0.18, tUp1 = v8 ? 0.62 : 0.72, tDn1 = v8 ? 1.42 : 1.55
    let cyl: [Double] = v8 ? [1.0, 0.93, 1.04, 0.96] : [1.0, 0.86, 1.08, 0.94, 1.03, 0.9]
    // resonators (frequency Hz, Q, gain): exhaust body, pipe, rasp
    let res: [(Double, Double, Double)] = v8 ? [(420, 2.2, 0.7), (1150, 3.5, 0.8), (2900, 4, 0.45)] : [(96, 1.6, 1.0), (310, 3.0, 0.62), (1250, 5.0, 0.24)]
    var y1 = [Double](repeating: 0, count: res.count), y2 = y1
    var coef = [(Double, Double, Double)]()
    for (f, q, g) in res { let r = exp(-Double.pi * (f / q) / rate); coef.append((2 * r * cos(2 * Double.pi * f / rate), r * r, g * (1 - r))) }
    var rasp1 = 0.0, rasp2 = 0.0
    let rf = v8 ? 4200.0 : 2600.0, rr = exp(-Double.pi * (rf / 3) / rate), rc1 = 2 * rr * cos(2 * Double.pi * rf / rate), rc2 = rr * rr
    var ib1 = 0.0, ib2 = 0.0
    let inf = v8 ? 2400.0 : 1150.0, ir = exp(-Double.pi * (inf / 2.2) / rate), ic1 = 2 * ir * cos(2 * Double.pi * inf / rate), ic2 = ir * ir
    var seed: UInt64 = v8 ? 0xC0FFEE1234 : 0x5EED54
    func noise() -> Double { seed ^= seed << 13; seed ^= seed >> 7; seed ^= seed << 17; return Double(seed % 20001) / 10000.0 - 1.0 }
    var phase = 0.0, lastFire = 0, burst = 0.0, env = 0.0, tonal = 0.0
    var out = [Double](repeating: 0, count: n)
    var lp = 0.0, hpX = 0.0, hpY = 0.0
    for i in 0..<n {
      let t = Double(i) / rate
      var rpm = idle, load = 0.3
      if t >= tUp0 && t < tUp1 { let x = (t - tUp0) / (tUp1 - tUp0); rpm = idle + (peak - idle) * (1 - pow(1 - x, 2.2)); load = 1.0 }
      else if t >= tUp1 && t < tDn1 { let x = (t - tUp1) / (tDn1 - tUp1); rpm = idle + (peak - idle) * exp(-3.4 * x) * (1 - x * 0.2); load = 0.14 }
      phase += rpm / 60.0 / rate * perRev   // firings so far
      let fireIdx = Int(phase)
      var x = 0.0
      if fireIdx != lastFire {
        lastFire = fireIdx
        let a = cyl[fireIdx % cyl.count] * (0.35 + 0.65 * load) * (0.6 + 0.4 * rpm / peak)
        x = a
        burst = a * (0.6 + 0.9 * load)
        if load < 0.2 && t > tUp1 + 0.08 && noise() > 0.86 { burst += 0.9 }   // overrun crackle
      }
      burst *= exp(-1.0 / (rate * (v8 ? 0.0012 : 0.0025)))
      var s = 0.0
      for j in 0..<res.count { let y = coef[j].0 * y1[j] - coef[j].1 * y2[j] + coef[j].2 * x * 60; y2[j] = y1[j]; y1[j] = y; s += y }
      let rn = noise() * burst
      let ry = rc1 * rasp1 - rc2 * rasp2 + rn * 0.08; rasp2 = rasp1; rasp1 = ry
      let fireHz = rpm / 60.0 * perRev
      tonal += 2 * Double.pi * fireHz / rate
      let tone = (sin(tonal) + 0.5 * sin(2 * tonal) + 0.28 * sin(3 * tonal) + 0.16 * sin(4 * tonal)) * (v8 ? 0.16 : 0.1) * (0.4 + 0.6 * load)
      let intakeEnv = 0.5 + 0.5 * sin(tonal)
      let iy = ic1 * ib1 - ic2 * ib2 + noise() * intakeEnv * load * (rpm / peak) * (v8 ? 0.02 : 0.045); ib2 = ib1; ib1 = iy
      var v = s + ry * (v8 ? 2.2 : 1.6) + tone + iy * 3
      lp += (v - lp) * (v8 ? 0.62 : 0.42)                 // gentle top roll-off
      v = lp
      hpY = 0.996 * (hpY + v - hpX); hpX = v              // DC / rumble block
      env = min(1, t / 0.02) * min(1, (secs - t) / 0.16)   // short fade in / out
      out[i] = hpY * env
    }
    let pk = max(out.map { abs($0) }.max() ?? 1, 1e-9), drive = 1.6
    return out.map { Float(tanh(drive * $0 / pk) / tanh(drive) * 0.7) }   // normalise, then a gentle soft clip
  }
  static func wav(_ k: Car) -> Data {
    let s = samples(k)
    var d = Data()
    func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
    func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
    d.append("RIFF".data(using: .ascii)!); u32(UInt32(36 + s.count * 2)); d.append("WAVEfmt ".data(using: .ascii)!)
    u32(16); u16(1); u16(1); u32(UInt32(rate)); u32(UInt32(rate) * 2); u16(2); u16(16)
    d.append("data".data(using: .ascii)!); u32(UInt32(s.count * 2))
    for v in s { u16(UInt16(bitPattern: Int16(max(-1, min(1, v)) * 32767))) }
    return d
  }
  static var cache = [Car: NSSound]()
  static var last = Date.distantPast
  /// Call only from a click handler.
  static func blip(_ k: Car) {
    guard on, Date().timeIntervalSince(last) > 2 else { return }
    last = Date()
    if cache[k] == nil { cache[k] = NSSound(data: wav(k)) }
    guard let snd = cache[k] else { return }
    snd.stop(); snd.volume = 0.5; snd.play()
  }
}
struct ScFile { var on = false, mode = "", target = 0, since = 0.0 }
func readScFile() -> ScFile {
  guard let d = try? Data(contentsOf: URL(fileURLWithPath: MGDIR + "/scramble.json")), let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return ScFile() }
  var f = ScFile()
  let until = (j["until"] as? Double) ?? 0
  f.on = (j["on"] as? Bool ?? false) && (until == 0 || until > Date().timeIntervalSince1970)
  f.mode = j["mode"] as? String ?? ""; if f.mode == "warthog" { f.mode = "cas" }   // the name before the rename; f.target = j["target_agents"] as? Int ?? 0; f.since = j["since"] as? Double ?? 0
  return f
}

// ---------------------------------------------------------------- tile icons (vector, no image files)
struct CarArt: View {   // the colour car of a mode, drawn by drawCar
  let car: Car; var height: CGFloat = 30
  var body: some View { Image(nsImage: carImage(car, height: height, template: false)).interpolation(.high) }
}
struct DRSWing: View {   // DRS: a rear wing seen from the side, the upper flap opens when DRS is on
  var on: Bool
  var body: some View {
    ZStack {
      RoundedRectangle(cornerRadius: 2).fill(Color.primary.opacity(0.75)).frame(width: 5, height: 30).offset(x: -15)   // endplates
      RoundedRectangle(cornerRadius: 2).fill(Color.primary.opacity(0.75)).frame(width: 5, height: 30).offset(x: 15)
      Capsule().fill(Color.primary.opacity(0.9)).frame(width: 30, height: 6).offset(y: 4)                                // main plane
      Capsule().fill(on ? Color.orange : Color.primary.opacity(0.9)).frame(width: 26, height: 5)
        .rotationEffect(.degrees(on ? -28 : -6)).offset(y: on ? -9 : -4)                                                  // flap
      if on { ForEach(0..<3) { i in Capsule().fill(Color.orange.opacity(0.6)).frame(width: 9, height: 1.5).offset(x: CGFloat(-4 + i * 4), y: CGFloat(-15 - i)) } }
    }.frame(width: 40, height: 38)
  }
}
struct StartLights: View {   // Lights Out: the five start lights on a dark gantry
  var body: some View {
    HStack(spacing: 2.5) { ForEach(0..<5) { _ in Circle().fill(RadialGradient(colors: [Color(red: 1, green: 0.3, blue: 0.25), Color(red: 0.6, green: 0.02, blue: 0.02)], center: .center, startRadius: 0, endRadius: 4)).frame(width: 6.5, height: 6.5) } }
      .padding(.horizontal, 4).padding(.vertical, 4).background(RoundedRectangle(cornerRadius: 3).fill(Color(white: 0.12))).frame(width: 44, height: 38)
  }
}
struct Sym: View {
  let name: String; let color: Color
  var body: some View { Image(systemName: name).font(.system(size: 26, weight: .semibold)).foregroundStyle(color).frame(height: 38) }
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
  switch mode { case "cap": return "Safety Car · \(n)"; case "surge": return "Quali Mode · \(n)"; case "cas": return "Full Send · \(n)"; case "custom": return "Custom · \(n)"; default: return "Race Pace · \(n)" }
}
func scCount(_ mode: String, _ custom: Int) -> Int { switch mode { case "cap": return 12; case "surge": return 25; case "cas": return 30; case "custom": return custom; default: return 20 } }
/// Agent-mode tile (same footprint as the other tiles): tap = start / stop; the label is a menu: Safety Car 12, Race Pace 20, Quali Mode 25, Full Send 30, Custom, Off.
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
    return m.st.sc ? "ON · \(hm(m.st.scLeft)) left\n\(n) agents · tap to stop" : "off · tap for 8 h\n\(n) agents on track"
  }
  func pick(_ mode: String) { m.pickMode(mode) }
  var body: some View {
    VStack(spacing: 5) {
      CarArt(car: carFor(mode: mode, target: n), height: carFor(mode: mode, target: n) == .e46 ? 30 : 22).frame(height: 46).opacity(m.st.sc ? 1 : 0.7)
      Menu {
        Button("Safety Car · 12 agents") { pick("cap") }
        Button("Race Pace · 20 agents") { pick("scramble") }
        Button("Quali Mode · 25 agents") { pick("surge") }.disabled(low)
        Button("Full Send · 30 agents") { pick("cas") }.disabled(low)
        Button("Custom · \(m.customN) agents") { pick("custom") }.disabled(low && m.customN > 20)
        Divider()
        Button("Off") { if m.st.sc { m.click(.scramble) } }.disabled(!m.st.sc)
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
    .onTapGesture { if enabled { m.click(.scramble) } }
    .opacity(enabled ? 1 : 0.45)
    .help("Tap to start or stop. Click the name to pick Safety Car 12, Race Pace 20, Quali Mode 25, Full Send 30 or Custom 1-40. Quali Mode, Full Send and Custom above 20 need 10 GB free disk.")
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
      Picker("", selection: $m.tab) { Text("Cockpit").tag(0); Text("Tower").tag(1); if QUEUE_ON { Text("Queue").tag(2) } }.pickerStyle(.segmented).labelsHidden()
      if m.tab == 2 && QUEUE_ON { queueView } else if m.tab == 1 { lanesView } else if let p = m.pending { confirm(p) } else { tiles }
      if m.busy != nil {
        HStack(spacing: 8) { ProgressView().controlSize(.small); Text(m.busyNote).font(.system(size: 11)).foregroundStyle(.secondary) }
      }
      if !m.result.isEmpty {
        Text(m.result).font(.system(size: 11, weight: .medium)).foregroundStyle(m.result.hasPrefix("Problem") ? Color.red : Color.primary)
          .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
      }
      Divider()
      Text("DRS \(m.st.ab ? "ON, \(hm(m.st.abLeft)) left" : "off") · tidy \(ago(m.st.lastTidy)) · save \(ago(m.st.lastSave)) · backup \(ago(m.st.lastBackup))")
        .font(.system(size: 10.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
      HStack {
        Toggle("Open at login", isOn: Binding(get: { m.loginOn }, set: { m.setLogin($0) })).toggleStyle(.switch).controlSize(.mini).font(.system(size: 11))
        Spacer()
        Button("Quit") { NSApp.terminate(nil) }.controlSize(.small)
      }
      HStack {
        Toggle("Engine sound (on click)", isOn: Binding(get: { m.soundOn }, set: { m.setSound($0) })).toggleStyle(.switch).controlSize(.mini).font(.system(size: 11))
        Spacer()
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
  /// Queue tab: running / finished today / pending tiles, a STALE banner (newest entry older than 2 h in business hours), the next three
  /// pending items, and how fresh the newest entry is.
  var queueView: some View {
    let q = m.queue
    let stale = queueStale(q, now: m.queueTick)
    let tint: Color = stale ? AMBER : .secondary
    func count(_ n: Int, _ label: String, _ c: Color) -> some View {
      VStack(spacing: 2) {
        Text(q.found ? "\(n)" : "–").font(.system(size: 28, weight: .bold, design: .rounded)).monospacedDigit()
        Text(label).font(.system(size: 10.5)).foregroundStyle(.secondary)
      }
      .frame(maxWidth: .infinity, minHeight: 68)
      .background(RoundedRectangle(cornerRadius: 12).fill(c.opacity(0.10)))
      .overlay(RoundedRectangle(cornerRadius: 12).stroke(c.opacity(0.28)))
    }
    return VStack(alignment: .leading, spacing: 8) {
      if stale {
        HStack(spacing: 6) {
          Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 12))
          Text("STALE · no queue entry for \(q.mtime.map { hm(Int(m.queueTick.timeIntervalSince($0) / 60)) } ?? "ever")").font(.system(size: 12, weight: .bold))
          Spacer(minLength: 0)
        }
        .foregroundStyle(AMBER).padding(.horizontal, 10).padding(.vertical, 7).frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 8).fill(AMBER.opacity(0.18)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(AMBER.opacity(0.7)))
      }
      HStack(spacing: 8) { count(q.running, "running", .green); count(q.finished, "finished today", .secondary); count(q.queued, "pending", .blue) }
      VStack(alignment: .leading, spacing: 5) {
        Text(q.top.isEmpty ? (q.found ? "Nothing pending" : "No queue found") : "Next up").font(.system(size: 11.5, weight: .semibold))
        ForEach(q.top) { it in
          HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(it.id).font(.system(size: 10, weight: .semibold, design: .monospaced)).foregroundStyle(.blue).frame(width: 28, alignment: .leading)
            Text(it.text).font(.system(size: 11)).lineLimit(2).truncationMode(.tail).fixedSize(horizontal: false, vertical: true)
          }
        }
      }
      .padding(10).frame(maxWidth: .infinity, alignment: .leading)
      .background(RoundedRectangle(cornerRadius: 12).fill(Color.blue.opacity(0.06)))
      .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.blue.opacity(0.2)))
      if q.found {
        HStack(spacing: 6) {
          Image(systemName: stale ? "exclamationmark.triangle.fill" : "clock").font(.system(size: 10)).foregroundStyle(stale ? AMBER : Color.secondary)
          Text("Newest entry \(q.mtime.map { ago($0.timeIntervalSince1970) } ?? "none")").lineLimit(1)
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
        Tile(title: "DRS", sub: m.st.ab ? "OPEN · \(hm(m.st.abLeft)) left\ntap to close" : "closed · tap for 2 h\nmore power", tint: .orange,
             enabled: idle, glow: m.st.ab, action: { m.click(.afterburner) }) { DRSWing(on: m.st.ab) }
        ScrambleTile(m: m, enabled: idle)
      }
      HStack(spacing: 8) {
        Tile(title: "Box Box", sub: "old build files\nto the Trash", tint: .brown, enabled: idle, glow: false, action: { m.click(.tidy) }) { Sym(name: "headphones", color: .brown) }
        Tile(title: "Lights Out", sub: "save + push\nall code to GitHub", tint: .red, enabled: idle, glow: false, action: { m.click(.save) }) { StartLights() }
      }
      HStack(spacing: 8) {
        Tile(title: "Telemetry", sub: m.st.drive.map { "backup to \($0)" } ?? "no drive", tint: .teal, enabled: idle && m.st.drive != nil, glow: false,
             action: { m.click(.backup) }) { Sym(name: "waveform.path.ecg", color: .teal) }
          .help(m.st.drive == nil ? (m.st.driveWhy.isEmpty ? "No backup drive attached" : m.st.driveWhy) : "Bundles + files to \(m.st.drive!)")
      }
      Button(action: { m.click(.all) }) {
        HStack(spacing: 10) {
          Sym(name: "wrench.and.screwdriver.fill", color: .orange).frame(width: 34)
          VStack(alignment: .leading, spacing: 1) {
            Text("Pit Stop").font(.system(size: 13, weight: .semibold))
            Text(m.st.drive == nil ? "all in one stop: box box → lights out → telemetry (no drive)" : "all in one stop: box box → lights out → telemetry").font(.system(size: 10.5)).foregroundStyle(.secondary)
          }
          Spacer()
        }
        .padding(.horizontal, 12).frame(maxWidth: .infinity, minHeight: 50)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.orange.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.orange.opacity(0.28)))
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
    item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    item.button?.target = self; item.button?.action = #selector(toggle)
    pop.behavior = .transient; pop.delegate = self
    pop.contentViewController = NSHostingController(rootView: PanelView(m: model))
    model.refresh(); updateIcon()
    Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in if !self.pop.isShown { self.model.refresh() } }
    timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.updateIcon() }
    scTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in self.watchScramble() }
  }
  // Agent-mode watcher (1 s, one tiny file read): the menu-bar car follows the mode (coupe for Safety Car / Race Pace, single-seater for
  // Quali Mode / Full Send). Nothing plays: Hangar has no screen animations, and sound only follows a click.
  var scTimer: Timer?
  var car = Car.e46, iconKey = ""
  func watchScramble() {
    let f = readScFile()
    let c: Car = f.on ? carFor(mode: f.mode, target: f.target) : .e46
    if c != car { car = c; updateIcon() }
  }
  func updateIcon() {
    guard let b = item?.button else { return }
    let on = model.st.ab, title = on ? " DRS \(hm(model.st.abLeft))" : ""
    let key = "\(car.rawValue)"
    if key != iconKey { iconKey = key; b.image = menuBarImage(car) }
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
// Screenshot helper: a fake menu bar around the real template image, at 1x or 2x.
struct MenuBarMock: View {
  let car: Car, drs: Bool
  var body: some View {
    let img = menuBarImage(car)
    HStack(spacing: 8) {
      Spacer(minLength: 0)
      Image(nsImage: img).renderingMode(.template)
      if drs { Text("DRS 1h 35m").font(.system(size: 13)) }
      Text("Wi-Fi  ·  Thu 2:22 PM").font(.system(size: 13)).foregroundStyle(.secondary)
    }
    .foregroundStyle(.primary).padding(.horizontal, 12).frame(width: 330, height: 24)
    .background(Color.primary.opacity(0.07))
  }
}
// ---------------------------------------------------------------- screenshots (HANGAR_SHOT=<dir>): offscreen, never a real status item, nothing plays
enum Shots {
  static var keep = [NSWindow]()
  static func render(_ v: NSView, _ path: String, scale: CGFloat = 2) {
    let b = v.bounds
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(b.width * scale), pixelsHigh: Int(b.height * scale), bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return }
    rep.size = b.size; v.cacheDisplay(in: b, to: rep)
    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
  }
  static func run(_ dir: String) {
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    exportArt(dir + "/art")
    let live = Model(); let (_, out) = runTool(TIDY, ["--status"]); if let s = parseStatus(out) { live.st = s }
    let ab = Model(); ab.st = live.st; ab.st.ab = true; ab.st.abLeft = 95; ab.result = "afterburner ON until 4:05 PM (2 h 00 min): Hangar button"
    let conf = Model(); conf.st = live.st
    let (_, plan) = runTool(TIDY, ["--dry-run", "--only", "clean"])
    conf.pending = Pending(act: .tidy, plan: plan.trimmingCharacters(in: .whitespacesAndNewlines), summary: summaryLine(plan))
    let ln = Model(); ln.st = live.st; ln.tab = 1; ln.lanes = readLanes()
    let sc1 = Model(); sc1.st = live.st; sc1.st.sc = true; sc1.st.scLeft = 262; sc1.st.scMode = "scramble"; sc1.st.scTarget = 20; sc1.st.scDiskLow = false
    let sc2 = Model(); sc2.st = live.st; sc2.st.sc = false; sc2.scPick = "custom"; sc2.customN = 14; sc2.st.scDiskLow = true; sc2.st.scDiskGB = 4.2
    let sc3 = Model(); sc3.st = live.st; sc3.st.sc = true; sc3.st.scLeft = 478; sc3.st.scMode = "cas"; sc3.st.scTarget = 30; sc3.st.scDiskLow = false
    sc3.result = "scramble ON (Full Send) until 10:20 PM (8h 0m): keep 30 agents running"
    let sc4 = Model(); sc4.st = live.st; sc4.st.sc = true; sc4.st.scLeft = 300; sc4.st.scMode = "cap"; sc4.st.scTarget = 12
    let sc5 = Model(); sc5.st = live.st; sc5.st.sc = true; sc5.st.scLeft = 300; sc5.st.scMode = "surge"; sc5.st.scTarget = 25
    let q1 = Model(); q1.st = live.st; q1.tab = 2; q1.queue = readQueue()
    let q2 = Model(); q2.st = live.st; q2.tab = 2; q2.queue = q1.queue; q2.queue.mtime = Date().addingTimeInterval(-135 * 60); q2.queueTick = Date()
    let cases: [(String, Model)] = [("queue", q1), ("queue_stale", q2), ("panel", live), ("drs_on", ab), ("confirm_boxbox", conf), ("lanes", ln),
                                    ("mode_safetycar", sc4), ("mode_racepace", sc1), ("mode_quali", sc5), ("mode_fullsend", sc3), ("mode_custom_disklow", sc2)]
    var jobs = [(AnyView, String, String, CGFloat)]()
    for (n, mdl) in cases { for ap in ["light", "dark"] { jobs.append((AnyView(PanelView(m: mdl)), ap, "\(dir)/hangar_\(n)_\(ap).png", 2)) } }
    for k in [Car.e46, .f1] { for ap in ["light", "dark"] { for s in [1, 2] as [CGFloat] {
      jobs.append((AnyView(MenuBarMock(car: k, drs: false)), ap, "\(dir)/hangar_menubar_\(k.rawValue)_\(ap)@\(Int(s))x.png", s))
      jobs.append((AnyView(PanelView(m: k == .e46 ? sc1 : sc3)), ap, "\(dir)/hangar_window_\(k.rawValue)_\(ap)@\(Int(s))x.png", s))
    } } }
    jobs.append((AnyView(MenuBarMock(car: .e46, drs: true)), "dark", "\(dir)/hangar_menubar_e46_drs_dark@2x.png", 2))
    func step(_ i: Int) {
      if i >= jobs.count { NSApp.terminate(nil); return }
      let (v, ap, path, s) = jobs[i]
      let a = NSAppearance(named: ap == "dark" ? .darkAqua : .aqua)
      let host = NSHostingView(rootView: v.background(Color(nsColor: .windowBackgroundColor)))
      host.appearance = a
      let size = host.fittingSize; host.frame = NSRect(origin: .zero, size: size)
      let w = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: size.width, height: size.height), styleMask: [.borderless], backing: .buffered, defer: false)
      w.appearance = a; w.contentView = host; w.orderFrontRegardless(); keep.append(w)
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { render(host, path, scale: s); w.orderOut(nil); step(i + 1) }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { step(0) }
  }
}

// Parser check without a window: HANGAR_QUEUE_PARSE=<file> prints running, finished, queued and the first ids as JSON and exits
// (a .jsonl file is folded as the agentq log, anything else is parsed as the markdown view). HANGAR_QUEUE_NOW=<epoch> fixes "now".
if let f = ENV["HANGAR_QUEUE_PARSE"] {
  let text = (try? String(contentsOfFile: f, encoding: .utf8)) ?? ""
  let now = Double(ENV["HANGAR_QUEUE_NOW"] ?? "").map { Date(timeIntervalSince1970: $0) } ?? Date()
  var j: [String: Any]
  if f.hasSuffix(".jsonl") {
    let q = foldQueueLog(text, now: now)
    j = ["running": q.running, "finished": q.finished, "queued": q.queued, "ids": q.top.map { $0.id }, "top": q.top.map { $0.text },
         "newest": q.mtime?.timeIntervalSince1970 ?? 0, "stale": queueStale(q, now: now)]
  } else {
    let r = parseQueue(text)
    j = ["running": r.running, "finished": r.finished, "queued": r.queued.count, "ids": r.queued.map { $0.id }, "top": r.queued.prefix(3).map { $0.text }]
  }
  print(String(decoding: (try? JSONSerialization.data(withJSONObject: j)) ?? Data(), as: UTF8.self)); exit(0)
}
// HANGAR_ENGINE_WAV=<file> [HANGAR_ENGINE=e46|f1] writes that car's click sound to a WAV and exits (nothing plays).
if let f = ENV["HANGAR_ENGINE_WAV"] {
  try? EngineSound.wav(Car(rawValue: ENV["HANGAR_ENGINE"] ?? "") ?? .e46).write(to: URL(fileURLWithPath: f)); exit(0)
}
// HANGAR_ART=<dir> writes the car art (SVG, PDF, 1x/2x PNG) and exits.
if let d = ENV["HANGAR_ART"] { exportArt(d); exit(0) }
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
