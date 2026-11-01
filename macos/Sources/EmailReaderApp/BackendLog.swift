import Darwin
import Foundation

/// backend.log outlives relaunches: the backend appends to it, and once it grows past
/// `maxBytes` it is shifted to backend.log.1 … backend.log.`keep` at the next launch.
enum BackendLog {
    static func open(in dir: URL, maxBytes: Int = 10 << 20, keep: Int = 5) -> FileHandle? {
        let fm = FileManager.default
        let log = dir.appendingPathComponent("backend.log")
        func rotated(_ n: Int) -> URL { dir.appendingPathComponent("backend.log.\(n)") }

        let size = ((try? fm.attributesOfItem(atPath: log.path))?[.size] as? Int) ?? 0
        if size > maxBytes {
            try? fm.removeItem(at: rotated(keep))
            for n in stride(from: keep - 1, through: 1, by: -1) {
                try? fm.moveItem(at: rotated(n), to: rotated(n + 1))
            }
            try? fm.moveItem(at: log, to: rotated(1))
        }

        let fd = Darwin.open(log.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        guard fd >= 0 else { return nil }
        return FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    }
}
