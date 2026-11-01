import AppKit
import UserNotifications
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKDownloadDelegate,
                         WKScriptMessageHandler, WKUIDelegate, NSWindowDelegate {
    private let backend = BackendController()
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var loadingLabel: NSTextField!
    private var notificationTimer: Timer?
    private var seenIDs = Set<String>()
    private var lastDownloadURL: URL?
    /// Windows opened from a letter's frame ("Open Frame in New Window"), kept alive
    /// while they are on screen.
    private var secondaryWindows: [NSWindow] = []

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        seenIDs = Set(UserDefaults.standard.stringArray(forKey: "seenNotificationIDs") ?? [])
        setupMenuBar()
        setupWindow()
        requestNotificationPermission()

        do {
            try backend.start()
        } catch {
            showFatal("Не удалось запустить бэкенд: \(error). Соберите его: pyinstaller packaging/email_reader_backend.spec")
            return
        }

        backend.waitUntilHealthy { [weak self] healthy in
            guard let self else { return }
            if healthy {
                self.webView.load(URLRequest(url: self.backend.baseURL))
                self.loadingLabel.isHidden = true
                self.startNotificationPolling()
            } else {
                self.showFatal("Бэкенд не ответил вовремя. Проверьте логи в \(self.backend.dataDir.path).")
            }
        }
    }

    /// Keep the backend (and background sync) running when the window is closed.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    /// Clicking the Dock icon after the window was closed re-shows it.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showWindow() }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        notificationTimer?.invalidate()
        backend.stop()
    }

    // MARK: - UI

    private func setupWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1100, height: 760)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Niti"
        // Programmatically-created NSWindows default to isReleasedWhenClosed=true:
        // the red close button would deallocate the window, leaving `self.window` a
        // dangling reference so "Открыть Niti" reopens nothing. Keep it alive — the
        // close button just hides it, and makeKeyAndOrderFront brings it back.
        window.isReleasedWhenClosed = false
        window.center()
        window.setFrameAutosaveName("MainWindow")

        let config = WKWebViewConfiguration()
        let controller = WKUserContentController()
        controller.add(self, name: "pickFolder")  // JS: webkit.messageHandlers.pickFolder
        config.userContentController = controller
        webView = WKWebView(frame: frame, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self  // route target="_blank" links to the system browser

        let container = NSView(frame: frame)
        container.addSubview(webView)

        loadingLabel = NSTextField(labelWithString: "Запуск бэкенда…")
        loadingLabel.alignment = .center
        loadingLabel.font = .systemFont(ofSize: 15)
        loadingLabel.frame = NSRect(x: 0, y: frame.height / 2 - 10, width: frame.width, height: 20)
        loadingLabel.autoresizingMask = [.width, .minYMargin, .maxYMargin]
        container.addSubview(loadingLabel)

        window.contentView = container
        showWindow()
    }

    private func setupMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // Niti "N" monogram as a menu-bar template image (macOS tints it for light/dark).
        // The PNG is dropped into Contents/Resources by packaging/build_app.sh; fall back
        // to a glyph when running unbundled (e.g. `swift run` in dev).
        if let path = Bundle.main.path(forResource: "MenuBarIcon", ofType: "png"),
           let icon = NSImage(contentsOfFile: path) {
            icon.size = NSSize(width: 18, height: 18)
            icon.isTemplate = true
            statusItem.button?.image = icon
        } else {
            statusItem.button?.title = "✉︎"
        }
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Открыть Niti", action: #selector(showWindow), keyEquivalent: "o"))
        menu.addItem(NSMenuItem(title: "Обновить", action: #selector(reload), keyEquivalent: "r"))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Выйти", action: #selector(quit), keyEquivalent: "q"))
        statusItem.menu = menu
    }

    @objc private func showWindow() {
        NSApp.setActivationPolicy(.regular)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func reload() {
        webView.reload()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func showFatal(_ message: String) {
        loadingLabel?.stringValue = message
        loadingLabel?.isHidden = false
    }

    // MARK: - Notifications & badge

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }
    }

    private func startNotificationPolling() {
        poll()
        notificationTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    private func poll() {
        let url = backend.baseURL.appendingPathComponent("api/notifications")
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self, let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            let counts = obj["counts"] as? [String: Int] ?? [:]
            let items = obj["items"] as? [[String: Any]] ?? []
            DispatchQueue.main.async {
                self.updateBadge(counts["today"] ?? 0)
                self.notifyNewItems(items)
            }
        }.resume()
    }

    private func updateBadge(_ count: Int) {
        NSApp.dockTile.badgeLabel = count > 0 ? String(count) : nil
    }

    private func notifyNewItems(_ items: [[String: Any]]) {
        for item in items {
            guard let id = item["id"] as? String, !seenIDs.contains(id) else { continue }
            seenIDs.insert(id)
            let title = item["title"] as? String ?? "Новая задача"
            let who = item["counterparty"] as? String
            let content = UNMutableNotificationContent()
            content.title = "Niti"
            content.body = who != nil ? "\(title) — \(who!)" : title
            let req = UNNotificationRequest(identifier: id, content: content, trigger: nil)
            UNUserNotificationCenter.current().add(req)
        }
        UserDefaults.standard.set(Array(seenIDs), forKey: "seenNotificationIDs")
    }

    // MARK: - Downloads (attachments)

    /// "Open" an attachment (/attachment/.../view) -> hand the localhost URL to the
    /// system default browser instead of navigating/downloading inside the app.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url {
            if Self.isAttachmentView(url) {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            // External links (e.g. "Open App Passwords in Google") -> system browser.
            if Self.isExternal(url) {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
        }
        decisionHandler(.allow)
    }

    /// An attachment asked to be opened: it belongs in the user's default browser, which
    /// knows what to do with a PDF or a spreadsheet, not in a window of ours.
    static func isAttachmentView(_ url: URL) -> Bool {
        url.path.contains("/attachment/") && url.path.hasSuffix("/view")
    }

    /// True for http(s) URLs that aren't the local backend (so they belong in the
    /// user's default browser, not the app's WebView).
    static func isExternal(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https"
        else { return false }
        let host = url.host ?? ""
        return host != "127.0.0.1" && host != "localhost"
    }

    /// target="_blank", window.open and the context menu's "Open Frame in New Window":
    /// WebKit asks the delegate for a view to load into. An external link goes to the
    /// system browser; our own page — a letter's HTML frame — gets a real second window.
    /// Returning nil for it made the menu item silently do nothing.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        if Self.isExternal(url) || Self.isAttachmentView(url) {
            NSWorkspace.shared.open(url)
            return nil
        }
        return openSecondaryWindow(configuration: configuration, features: windowFeatures)
    }

    /// A second window for a page of our own backend. WebKit loads the request into the
    /// view it gets back, so nothing is loaded here. The window must use the very
    /// configuration WebKit handed over — a fresh one makes WebKit drop the request.
    private func openSecondaryWindow(configuration: WKWebViewConfiguration,
                                     features: WKWindowFeatures) -> WKWebView {
        let frame = NSRect(x: 0, y: 0,
                           width: features.width?.doubleValue ?? 900,
                           height: features.height?.doubleValue ?? 700)
        let view = WKWebView(frame: frame, configuration: configuration)
        view.autoresizingMask = [.width, .height]
        view.navigationDelegate = self
        view.uiDelegate = self

        let secondary = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        secondary.title = "Niti"
        secondary.contentView = view
        secondary.center()
        // Held here until it closes: a programmatic window released on close would be
        // deallocated under the reference this array keeps (as for the main window).
        secondary.isReleasedWhenClosed = false
        secondary.delegate = self
        secondaryWindows.append(secondary)
        secondary.makeKeyAndOrderFront(nil)
        return view
    }

    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow else { return }
        secondaryWindows.removeAll { $0 === closing }
    }

    // MARK: - JavaScript dialogs (alert / confirm / prompt)
    // Without these WKUIDelegate methods WKWebView silently drops window.alert() and,
    // crucially, makes window.confirm() return false — which made every hx-confirm
    // button (delete label/account/model connection, reset prompt, purge storage) do
    // nothing in the packaged app (it works in a plain browser, hence dev-only pass).

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        if let win = webView.window {
            alert.beginSheetModal(for: win) { _ in completionHandler() }
        } else {
            alert.runModal()
            completionHandler()
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Отмена")
        if let win = webView.window {
            alert.beginSheetModal(for: win) { resp in
                completionHandler(resp == .alertFirstButtonReturn)
            }
        } else {
            completionHandler(alert.runModal() == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Отмена")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        input.stringValue = defaultText ?? ""
        alert.accessoryView = input
        let finish: (NSApplication.ModalResponse) -> Void = { resp in
            completionHandler(resp == .alertFirstButtonReturn ? input.stringValue : nil)
        }
        if let win = webView.window {
            alert.beginSheetModal(for: win, completionHandler: finish)
        } else {
            finish(alert.runModal())
        }
    }

    // MARK: - File picker
    // Without this WKUIDelegate method a click on <input type="file"> does nothing at
    // all in the packaged app: the webview has no picker of its own. Same dev-only trap
    // as the JS dialogs above — «Прикрепить» works in a browser and is dead in the .app.

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        let finish: (NSApplication.ModalResponse) -> Void = { resp in
            completionHandler(resp == .OK ? panel.urls : nil)
        }
        if let win = webView.window {
            panel.beginSheetModal(for: win, completionHandler: finish)
        } else {
            finish(panel.runModal())
        }
    }

    /// Treat /attachment/ links, anything ending in /download, and anything the webview
    /// can't render as downloads. The /download suffix matters for text/plain (a meeting
    /// transcript): the webview CAN render it, and would replace the whole app with it.
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let path = navigationResponse.response.url?.path ?? ""
        if path.contains("/attachment/") || path.hasSuffix("/download")
            || !navigationResponse.canShowMIMEType {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var dest = downloads.appendingPathComponent(suggestedFilename)
        // Avoid clobbering an existing file: name, name-1, name-2, ...
        let fm = FileManager.default
        if fm.fileExists(atPath: dest.path) {
            let ext = dest.pathExtension
            let stem = dest.deletingPathExtension().lastPathComponent
            var i = 1
            repeat {
                let candidate = ext.isEmpty ? "\(stem)-\(i)" : "\(stem)-\(i).\(ext)"
                dest = downloads.appendingPathComponent(candidate)
                i += 1
            } while fm.fileExists(atPath: dest.path)
        }
        lastDownloadURL = dest
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = lastDownloadURL {
            NSWorkspace.shared.activateFileViewerSelecting([url])  // reveal in Finder
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error,
                  resumeData: Data?) {
        NSLog("Attachment download failed: \(error.localizedDescription)")
    }

    // MARK: - Native folder picker (JS bridge for Projects)

    func userContentController(_ userContentController: WKUserContentController,
                              didReceive message: WKScriptMessage) {
        guard message.name == "pickFolder" else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true   // user can make a new project folder here
        panel.allowsMultipleSelection = false
        panel.prompt = "Выбрать"
        panel.message = "Выберите или создайте папку для проекта"
        panel.begin { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            // Write the chosen POSIX path back into the form field.
            let escaped = url.path
                .replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "'", with: "\\'")
            self?.webView.evaluateJavaScript("window.__setProjectFolder('\(escaped)')")
        }
    }
}
