import Testing
@testable import sttCore

@Suite("Bundle attribution")
struct BundleAttributionTests {

    @Test func detectsAppBundlePath() {
        #expect(BundleAttribution.isRunningFromAppBundle(bundlePath: "/Applications/stt.app"))
        #expect(BundleAttribution.isRunningFromAppBundle(bundlePath: "/tmp/stt.app"))
        #expect(BundleAttribution.isRunningFromAppBundle(bundlePath: "/usr/local/bin/stt") == false)
        #expect(BundleAttribution.isRunningFromAppBundle(bundlePath: nil) == false)
        #expect(BundleAttribution.isRunningFromAppBundle(bundlePath: "") == false)
    }

    @Test func appBundleDiagnosticMentionsBundleIdentifier() {
        let lines = BundleAttribution.diagnosticLines(
            bundlePath: "/Applications/stt.app",
            bundleIdentifier: "com.example.stt"
        )

        #expect(lines.contains { $0.contains("inside a .app bundle") })
        #expect(lines.contains("Bundle identifier: com.example.stt"))
    }

    @Test func bareBinaryDiagnosticIncludesBuildInstructions() {
        let lines = BundleAttribution.diagnosticLines(bundlePath: "/usr/local/bin/stt", bundleIdentifier: nil)
        let joined = lines.joined(separator: "\n")

        #expect(joined.contains("bare binary"))
        #expect(joined.contains("./scripts/build-app-bundle.sh"))
        #expect(joined.contains("./dist/stt.app/Contents/MacOS/stt doctor"))
    }
}
