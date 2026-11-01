import Darwin
import Foundation

/// Owns the lifecycle of the bundled Python backend (the "goosed" equivalent):
/// picks a free port, launches the PyInstaller binary as a child process, and
/// waits for it to start serving HTTP.
final class BackendController {
    private(set) var port: Int = 0
    private var process: Process?

    /// ~/Library/Application Support/EmailReader — DB, config.yaml, .env live here.
    /// Имя каталога берётся из Info.plist (NitiDataDirectory), чтобы другая сборка с
    /// другим bundle id не открыла чужую базу. EMAIL_READER_DATA_DIR перекрывает путь —
    /// тем же именем, что понимает бэкенд: так оболочку можно запустить на отдельном
    /// каталоге, не трогая боевую базу.
    var dataDir: URL {
        if let env = ProcessInfo.processInfo.environment["EMAIL_READER_DATA_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: env, isDirectory: true)
        }
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let name = Bundle.main.object(forInfoDictionaryKey: "NitiDataDirectory") as? String
        return base.appendingPathComponent(name ?? "EmailReader", isDirectory: true)
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    /// Locate the backend executable: inside the .app's Resources when bundled,
    /// or via $BACKEND_BIN / the dev dist/ path when running from SwiftPM.
    private func backendExecutable() -> URL? {
        if let env = ProcessInfo.processInfo.environment["BACKEND_BIN"] {
            return URL(fileURLWithPath: env)
        }
        if let res = Bundle.main.resourceURL {
            let bundled = res
                .appendingPathComponent("backend", isDirectory: true)
                .appendingPathComponent("email-reader-backend")
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return bundled
            }
        }
        // Dev fallback: <repo>/dist/email-reader-backend/email-reader-backend
        let dev = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent("dist/email-reader-backend/email-reader-backend")
        if FileManager.default.isExecutableFile(atPath: dev.path) {
            return dev
        }
        return nil
    }

    /// Порт, на котором слушает бэкенд, должен быть один и тот же от запуска к запуску:
    /// его адрес попадает в настройки внешнего агента (MCP), а localStorage веб-интерфейса
    /// привязан к origin — при плавающем порте ширина панели и свёрнутые ящики сбрасываются
    /// каждый запуск. Поэтому сначала пробуем прошлый порт, потом свой диапазон, и только
    /// если всё занято — любой свободный, чтобы приложение всё-таки открылось. Начало
    /// диапазона — из Info.plist (NitiPortBase): две сборки рядом не делят порт и адрес MCP.
    private static let portRange: ClosedRange<Int> = {
        let base = Bundle.main.object(forInfoDictionaryKey: "NitiPortBase") as? Int ?? 8765
        return base...(base + 10)
    }()
    private static let lastPortKey = "backendPort"

    private func choosePort() -> Int {
        var candidates: [Int] = []
        let saved = UserDefaults.standard.integer(forKey: Self.lastPortKey)
        if Self.portRange.contains(saved) { candidates.append(saved) }
        candidates += Self.portRange.filter { $0 != saved }
        for port in candidates where isFree(port) { return port }
        return findFreePort()
    }

    /// Свободен ли порт: занимаем и сразу отпускаем. Между проверкой и запуском бэкенда
    /// порт теоретически может перехватить другой процесс — тогда бэкенд не поднимется и
    /// `waitUntilHealthy` честно скажет об этом, а следующий запуск возьмёт соседний.
    ///
    /// SO_REUSEADDR обязателен: сразу после выхода из приложения на его порте висят
    /// соединения в TIME_WAIT, и проба без этого флага объявляет порт занятым, хотя
    /// uvicorn (он ставит флаг сам) на нём поднялся бы. Иначе перезапуск в первую минуту
    /// уводил бы приложение на соседний порт — ровно то, от чего фиксированный порт и
    /// заводился.
    private func isFree(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return bound == 0
    }

    /// Ask the OS for an unused TCP port by binding to port 0 and reading it back.
    private func findFreePort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return 8000 }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return 8000 }
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafeMutablePointer(to: &addr) {
            _ = $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &len)
            }
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    /// Файл с адресом бэкенда рядом с базой: по нему внешний агент или скрипт узнаёт
    /// порт, не заглядывая в Настройки. Пишется, когда сервер уже отвечает, стирается
    /// при запуске и при выходе. После аварийного завершения строка может остаться —
    /// её перепишет следующий запуск, и порт в ней, скорее всего, тот же самый.
    private var urlFile: URL { dataDir.appendingPathComponent("backend-url.txt") }

    private func publishURL() {
        try? baseURL.absoluteString.appending("\n")
            .write(to: urlFile, atomically: true, encoding: .utf8)
    }

    enum StartError: Error { case executableNotFound }

    /// Launch the backend. Throws if the binary is missing.
    func start() throws {
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        try? FileManager.default.removeItem(at: urlFile)
        guard let exe = backendExecutable() else { throw StartError.executableNotFound }

        port = choosePort()
        let proc = Process()
        proc.executableURL = exe
        proc.arguments = ["app"]
        var env = ProcessInfo.processInfo.environment
        env["EMAIL_READER_DATA_DIR"] = dataDir.path
        env["WEB_PORT"] = String(port)
        env["WEB_HOST"] = "127.0.0.1"
        proc.environment = env

        // Capture backend stdout/stderr to a log file so errors are visible
        // (the .app has no console).
        if let handle = BackendLog.open(in: dataDir) {
            proc.standardOutput = handle
            proc.standardError = handle
        }

        try proc.run()
        process = proc
    }

    /// Poll the HTTP server until it answers 200 or the deadline passes.
    /// onResult is called on the main queue with whether the backend came up.
    func waitUntilHealthy(timeout: TimeInterval = 40, onResult: @escaping (Bool) -> Void) {
        let deadline = Date().addingTimeInterval(timeout)
        let url = baseURL.appendingPathComponent("view/today")

        func poll() {
            var req = URLRequest(url: url)
            req.timeoutInterval = 2
            URLSession.shared.dataTask(with: req) { _, response, _ in
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    UserDefaults.standard.set(self.port, forKey: Self.lastPortKey)
                    self.publishURL()
                    DispatchQueue.main.async { onResult(true) }
                    return
                }
                if Date() > deadline {
                    DispatchQueue.main.async { onResult(false) }
                    return
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.5) { poll() }
            }.resume()
        }
        poll()
    }

    /// Terminate the child process (called on app quit).
    func stop() {
        try? FileManager.default.removeItem(at: urlFile)
        guard let proc = process, proc.isRunning else { return }
        proc.terminate()
        // Give it a moment to flush/close the DB; SIGKILL if it lingers.
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
        }
    }
}
