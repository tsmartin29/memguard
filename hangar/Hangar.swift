import AppKit
import SwiftUI
import ServiceManagement
// Hangar: a menu-bar remote for memguard's own commands, with an F-22 button for afterburner. It runs these commands and shows their
// result line, nothing else:
//   afterburner on 2h / off  ·  tidy --only clean  ·  tidy --only save  ·  tidy --only backup  ·  tidy (all three)
// Tidy, Backup and Everything first run `tidy --dry-run ...`, show that plan and wait for one Confirm. No network of its own, no admin
// rights. "Open at login" is OFF until you switch it on in the panel (SMAppService). Build: ./build.sh  Screenshots: HANGAR_SHOT=<dir>.
// The commands default to ~/.local/bin/tidy and ~/.local/bin/afterburner (install.sh links them there); HANGAR_TIDY and
// HANGAR_AFTERBURNER override the paths.

let HOMEDIR = NSHomeDirectory()
let ENV = ProcessInfo.processInfo.environment
let TIDY = ENV["HANGAR_TIDY"] ?? (HOMEDIR + "/.local/bin/tidy")
let AFTERBURNER = ENV["HANGAR_AFTERBURNER"] ?? (HOMEDIR + "/.local/bin/afterburner")

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

struct Status {
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

enum Act { case afterburner, tidy, save, backup, all
  var title: String { switch self { case .afterburner: return "Afterburner"; case .tidy: return "Tidy"; case .save: return "Save + push"
    case .backup: return "Backup"; case .all: return "Everything" } }
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

  func refresh() {
    DispatchQueue.global(qos: .userInitiated).async {
      let (_, out) = runTool(TIDY, ["--status"])
      if let s = parseStatus(out) { DispatchQueue.main.async { self.st = s } }
    }
  }
  func tap(_ a: Act) {
    guard busy == nil else { return }
    switch a {
    case .afterburner: run(a, AFTERBURNER, st.ab ? ["off", "turned off in Hangar"] : ["on", "2h", "Hangar button"])
    case .save: run(a, TIDY, a.tidyArgs)
    case .tidy, .backup, .all: preview(a)
    }
  }
  func preview(_ a: Act) {
    busy = a; busyNote = "Checking what \(a.title.lowercased()) would do…"; result = ""
    DispatchQueue.global(qos: .userInitiated).async {
      let (_, out) = runTool(TIDY, ["--dry-run"] + a.tidyArgs)
      DispatchQueue.main.async { self.busy = nil; self.pending = Pending(act: a, plan: out.trimmingCharacters(in: .whitespacesAndNewlines), summary: summaryLine(out)) }
    }
  }
  func confirm() { guard let p = pending else { return }; pending = nil; run(p.act, TIDY, p.act.tidyArgs) }
  func run(_ a: Act, _ tool: String, _ args: [String]) {
    busy = a; result = ""
    busyNote = (a == .backup || a == .all) ? "Working… the backup waits its turn in the memguard queue" : "Working…"
    DispatchQueue.global(qos: .userInitiated).async {
      let (rc, out) = runTool(tool, args)
      DispatchQueue.main.async { self.busy = nil; self.result = (rc == 0 ? "" : "Problem: ") + summaryLine(out); self.refresh() }
    }
  }
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
struct Broom: View {
  var body: some View {
    BroomShape().fill(LinearGradient(colors: [Color(red: 0.62, green: 0.42, blue: 0.22), Color(red: 0.86, green: 0.68, blue: 0.32)],
                                     startPoint: .topTrailing, endPoint: .bottomLeading)).frame(width: 38, height: 38)
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
      if let p = m.pending { confirm(p) } else { tiles }
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
    }
    .padding(14).frame(width: 344)
  }
  var tiles: some View {
    let idle = m.busy == nil
    return VStack(spacing: 8) {
      HStack(spacing: 8) {
        Tile(title: "Afterburner", sub: m.st.ab ? "ON · \(hm(m.st.abLeft)) left\ntap to land" : "off · tap for 2 h\nmore power", tint: .orange,
             enabled: idle, glow: m.st.ab, action: { m.tap(.afterburner) }) { Jet(on: m.st.ab) }
        Tile(title: "Tidy", sub: "old build files\nto the Trash", tint: .brown, enabled: idle, glow: false, action: { m.tap(.tidy) }) { Broom() }
      }
      HStack(spacing: 8) {
        Tile(title: "Save + push", sub: "all code\nto GitHub", tint: .blue, enabled: idle, glow: false, action: { m.tap(.save) }) {
          Sym(name: "icloud.and.arrow.up.fill", color: .blue) }
        Tile(title: "Backup", sub: m.st.drive.map { "to \($0)" } ?? "no drive", tint: .teal, enabled: idle && m.st.drive != nil, glow: false,
             action: { m.tap(.backup) }) { Sym(name: "externaldrive.fill", color: .teal) }
          .help(m.st.drive == nil ? (m.st.driveWhy.isEmpty ? "No backup drive attached" : m.st.driveWhy) : "Bundles + files to \(m.st.drive!)")
      }
      Button(action: { m.tap(.all) }) {
        HStack(spacing: 10) {
          Image(systemName: "sparkles").font(.system(size: 20, weight: .semibold)).foregroundStyle(.purple)
          VStack(alignment: .leading, spacing: 1) {
            Text("Do all three").font(.system(size: 13, weight: .semibold))
            Text(m.st.drive == nil ? "tidy → save → backup (skipped: no drive)" : "tidy → save → backup").font(.system(size: 10.5)).foregroundStyle(.secondary)
          }
          Spacer()
        }
        .padding(.horizontal, 12).frame(maxWidth: .infinity, minHeight: 50)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.purple.opacity(0.10)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.purple.opacity(0.28)))
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
  }
  func updateIcon() {
    guard let b = item?.button else { return }
    let on = model.st.ab
    let img = NSImage(systemSymbolName: on ? "airplane.departure" : "airplane", accessibilityDescription: "Hangar")
    img?.isTemplate = true; b.image = img
    b.title = on ? " \(hm(model.st.abLeft))" : ""
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
  }
  // Opening the app again (Finder/Spotlight) shows the panel centered, in case the menu bar plane is hidden behind the notch.
  var win: NSWindow?
  func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { showWindow(nil); return false }
  func popoverDidClose(_ n: Notification) {
    timer?.invalidate(); timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.updateIcon() }
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
  static func run(_ dir: String) {
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    let live = Model(); let (_, out) = runTool(TIDY, ["--status"]); if let s = parseStatus(out) { live.st = s }
    let ab = Model(); ab.st = live.st; ab.st.ab = true; ab.st.abLeft = 95; ab.result = "afterburner ON until 4:05 PM (2 h 00 min): Hangar button"
    let conf = Model(); conf.st = live.st
    let (_, plan) = runTool(TIDY, ["--dry-run", "--only", "clean"])
    conf.pending = Pending(act: .tidy, plan: plan.trimmingCharacters(in: .whitespacesAndNewlines), summary: summaryLine(plan))
    let cases: [(String, Model)] = [("panel", live), ("afterburner_on", ab), ("confirm_tidy", conf)]
    var jobs = [(Model, String, String)]()
    for (n, mdl) in cases { for ap in ["light", "dark"] { jobs.append((mdl, ap, "\(dir)/hangar_\(n)_\(ap).png")) } }
    func step(_ i: Int) {
      if i >= jobs.count { NSApp.terminate(nil); return }
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

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
