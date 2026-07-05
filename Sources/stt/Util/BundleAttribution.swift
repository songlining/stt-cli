import Foundation

/// Pure helpers for reasoning about whether the current executable is running
/// from a macOS `.app` bundle. Keeping this separate from `Doctor.run()` makes
/// the Phase 1 TCC attribution guidance unit-testable without prompting for any
/// permissions.
public enum BundleAttribution {
    public static let recommendedBundleIdentifier = "com.larrysong.stt"

    public static func isRunningFromAppBundle(bundlePath: String?) -> Bool {
        guard let bundlePath, !bundlePath.isEmpty else { return false }
        return bundlePath.hasSuffix(".app")
    }

    public static func diagnosticLines(bundlePath: String?, bundleIdentifier: String?) -> [String] {
        if isRunningFromAppBundle(bundlePath: bundlePath), let bundlePath {
            let identifier = bundleIdentifier ?? recommendedBundleIdentifier
            return [
                "Bundle: running inside a .app bundle (\(bundlePath))  [OK for correct TCC attribution]",
                "Bundle identifier: \(identifier)"
            ]
        }

        return [
            "Bundle: running as a bare binary (not inside a .app bundle).",
            "  [WARNING] TCC (microphone/audio-capture) permission prompts may be attributed to your",
            "  terminal application rather than to `stt`. Build and run the bundled app wrapper:",
            "    ./scripts/build-app-bundle.sh",
            "    ./dist/stt.app/Contents/MacOS/stt doctor",
            "  This gives macOS a stable bundle identifier for stt-specific permissions."
        ]
    }
}
