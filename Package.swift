// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "stt",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.3.0")
    ],
    targets: [
        .target(
            name: "sttCore",
            dependencies: [
                .product(name: "ArgumentParser", package: "swift-argument-parser")
            ],
            path: "Sources/stt",
            exclude: ["main.swift"]
        ),
        .executableTarget(
            name: "stt",
            dependencies: [
                "sttCore",
                .product(name: "ArgumentParser", package: "swift-argument-parser")
            ],
            path: "Sources/stt",
            exclude: ["Audio", "CLI", "Permissions", "Transcription", "Util"],
            sources: ["main.swift"]
        ),
        .executableTarget(
            name: "sttUnitChecks",
            dependencies: [
                "sttCore",
                .product(name: "ArgumentParser", package: "swift-argument-parser")
            ],
            path: "Sources/sttUnitChecks"
        ),
        .testTarget(
            name: "sttTests",
            dependencies: [
                "sttCore",
                .product(name: "ArgumentParser", package: "swift-argument-parser")
            ],
            path: "Tests/sttTests",
            swiftSettings: [
                .unsafeFlags(["-F", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks"])
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-F", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                    "-Xlinker", "-rpath", "-Xlinker", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                    "-Xlinker", "-rpath", "-Xlinker", "/Library/Developer/CommandLineTools/Library/Developer/usr/lib"
                ])
            ]
        )
    ]
)
