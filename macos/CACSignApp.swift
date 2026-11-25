import AppKit
import SwiftUI
import UniformTypeIdentifiers

public enum CACSignFilePanel {
    @MainActor
    public static func presentOpenPanel(vm: WorkspaceViewModel) {
        let panel = NSOpenPanel()
        var types: [UTType] = [.pdf]
        if let docxType = UTType(filenameExtension: "docx") {
            types.append(docxType)
        }
        panel.allowedContentTypes = types
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true
        panel.message = "Select PDF or Word (.docx) forms to inspect, fill, and CAC-sign"
        panel.begin { response in
            guard response == .OK, !panel.urls.isEmpty else { return }
            let urls = panel.urls
            Task { @MainActor in
                await vm.openURLs(urls)
            }
        }
    }
}

@MainActor
public final class CACSignAppDelegate: NSObject, NSApplicationDelegate {
    public static weak var sharedVM: WorkspaceViewModel?
    private static var pendingOpenURLs: [URL] = []
    private static var pendingQuickSignURLs: [URL] = []

    public static func bind(vm: WorkspaceViewModel) {
        sharedVM = vm
        if !pendingOpenURLs.isEmpty {
            let queued = pendingOpenURLs
            pendingOpenURLs.removeAll()
            Task { @MainActor in
                await vm.openURLs(queued)
            }
        }
        if !pendingQuickSignURLs.isEmpty {
            let quickQueued = pendingQuickSignURLs
            pendingQuickSignURLs.removeAll()
            Task { @MainActor in
                await vm.quickSignFromService(quickQueued)
            }
        }
    }

    public func applicationDidFinishLaunching(_ notification: Notification) {
        ServicesProvider.shared.onReviewFiles = { urls in
            Task { @MainActor in
                NSApp.activate(ignoringOtherApps: true)
                if let vm = Self.sharedVM {
                    await vm.openURLs(urls)
                } else {
                    Self.pendingOpenURLs.append(contentsOf: urls)
                }
            }
        }

        ServicesProvider.shared.onQuickSignFiles = { urls in
            Task { @MainActor in
                NSApp.activate(ignoringOtherApps: true)
                if let vm = Self.sharedVM {
                    await vm.quickSignFromService(urls)
                } else {
                    Self.pendingQuickSignURLs.append(contentsOf: urls)
                }
            }
        }

        NSApp.servicesProvider = ServicesProvider.shared

        if CommandLine.arguments.contains("--smoke-test") {
            Task { @MainActor in
                let targetVM = Self.sharedVM ?? WorkspaceViewModel()
                await targetVM.loadBootstrapState()
                let payload: [String: Any] = [
                    "smoke_ok": true,
                    "personas_count": targetVM.personasState.personas.count,
                    "profiles_count": targetVM.profiles.count,
                    "dod_roots_trusted": targetVM.diagnostics?.dod_roots_trusted ?? TokenMonitor.shared.status.dodRootsTrusted
                ]
                if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
                   let jsonLine = String(data: data, encoding: .utf8) {
                    FileHandle.standardOutput.write(Data((jsonLine + "\n").utf8))
                } else {
                    FileHandle.standardOutput.write(Data("{\"smoke_ok\":true}\n".utf8))
                }
                await DesktopBridge.shared.shutdown()
                NSApp.terminate(nil)
            }
        }
    }

    public func application(_ application: NSApplication, open urls: [URL]) {
        guard !urls.isEmpty else { return }
        if let vm = Self.sharedVM {
            Task { @MainActor in
                await vm.openURLs(urls)
            }
        } else {
            Self.pendingOpenURLs.append(contentsOf: urls)
        }
    }

    public func applicationWillTerminate(_ notification: Notification) {
        Task {
            await DesktopBridge.shared.shutdown()
        }
    }
}

@main
public struct CACSignApp: App {
    @NSApplicationDelegateAdaptor(CACSignAppDelegate.self) private var appDelegate
    @StateObject private var vm = WorkspaceViewModel()

    public init() {}

    public var body: some Scene {
        WindowGroup("CAC Sign") {
            MainWindowView(vm: vm)
                .task {
                    CACSignAppDelegate.bind(vm: vm)
                    if !CommandLine.arguments.contains("--smoke-test") {
                        await vm.loadBootstrapState()
                    }
                }
        }
        .windowStyle(.titleBar)
        .defaultSize(width: 1280, height: 840)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Open PDF or DOCX…") {
                    CACSignFilePanel.presentOpenPanel(vm: vm)
                }
                .keyboardShortcut("o", modifiers: .command)
            }

            CommandGroup(after: .saveItem) {
                Button("Save Filled Copy") {
                    Task { await vm.saveFilledCopy() }
                }
                .keyboardShortcut("s", modifiers: .command)
                .disabled(vm.currentSession == nil || vm.isBusy)

                Button("Review & Sign…") {
                    Task { await vm.beginSignReview() }
                }
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(vm.currentSession == nil || (vm.currentSession?.isBlocked ?? true) || !(vm.currentSession?.hasSignTarget ?? false) || vm.isBusy)
            }

            CommandGroup(replacing: .undoRedo) {
                Button("Undo") { vm.undo() }
                    .keyboardShortcut("z", modifiers: .command)
                    .disabled(!vm.canUndo)

                Button("Redo") { vm.redo() }
                    .keyboardShortcut("z", modifiers: [.command, .shift])
                    .disabled(!vm.canRedo)
            }

            CommandMenu("Form") {
                Button("Fill Matched Blank Fields") {
                    vm.fillAllMatched()
                }
                .keyboardShortcut("k", modifiers: .command)
                .disabled(vm.currentSession == nil || vm.isBusy)

                Button("Paste Patient from Clipboard (RAM Only)") {
                    vm.pastePatientFromClipboard()
                }
                .keyboardShortcut("v", modifiers: [.command, .shift])

                Button("Clear Ephemeral Patient") {
                    vm.clearPatient()
                }
                .disabled(vm.patientContext == nil)

                Divider()

                Button("Suggest from Clinical Note…") {
                    vm.showAdvisoryNoteSheet = true
                }
                .disabled(vm.currentSession == nil || vm.isBusy)

                Button("Remember This Form…") {
                    vm.showProfileBuilderSheet = true
                }
                .disabled(vm.currentSession == nil)

                Divider()

                Button("Zoom In") { vm.adjustZoom(delta: 0.15) }
                    .keyboardShortcut("=", modifiers: .command)
                Button("Zoom Out") { vm.adjustZoom(delta: -0.15) }
                    .keyboardShortcut("-", modifiers: .command)
                Button("Actual Size (100%)") { vm.resetZoom() }
                    .keyboardShortcut("0", modifiers: .command)
            }
        }

        Settings {
            SettingsView(vm: vm)
        }
    }
}
