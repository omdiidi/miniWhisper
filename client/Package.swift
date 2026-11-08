// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "WisprAlt",
    platforms: [
        .macOS(.v15)
    ],
    products: [
        .executable(name: "WisprAlt", targets: ["WisprAlt"])
    ],
    dependencies: [
        // Sparkle 2.x — EdDSA-signed auto-update framework
        .package(
            url: "https://github.com/sparkle-project/Sparkle",
            branch: "2.x"
        )
    ],
    targets: [
        // Pure-Swift library of injection-decision predicates and focus context
        // data. Intentionally has no AppKit / ApplicationServices / Sparkle /
        // resource dependencies so it can be unit-tested with `swift test`
        // without invoking actool or pulling Sparkle's framework into the test
        // runner. See Task 8.6 in tmp/ready-plans/2026-04-28-fix-injection-imessages-pane.md.
        .target(
            name: "WisprAltCore",
            path: "WisprAltCore",
            swiftSettings: [
                .swiftLanguageMode(.v5)
            ]
        ),
        .executableTarget(
            name: "WisprAlt",
            dependencies: [
                "WisprAltCore",
                .product(name: "Sparkle", package: "Sparkle")
            ],
            path: "WisprAlt",
            resources: [
                .process("Resources/Assets.xcassets")
            ],
            swiftSettings: [
                // Strict concurrency was originally enabled but the whole codebase
                // wasn't audited for Sendable conformance — turning off until we
                // do a proper concurrency-safety pass.
                .swiftLanguageMode(.v5)
            ],
            linkerSettings: [
                // SPM does NOT add an @executable_path/../Frameworks rpath for
                // executable targets, so the bundled Sparkle.framework would
                // fail to load at runtime ("Library not loaded: @rpath/Sparkle").
                // Adding it here means both build-client-local.sh (ad-hoc) and
                // build-client.sh (xcodebuild via SPM project) inherit the fix.
                .unsafeFlags([
                    "-Xlinker", "-rpath",
                    "-Xlinker", "@executable_path/../Frameworks"
                ])
            ]
        ),
        // Real-API assumption gates (scripts/online-assumptions/run-all.sh).
        // A developer tool: never bundled (build-client-local.sh copies only
        // the WisprAlt executable).
        .executableTarget(
            name: "WisprAltProbe",
            dependencies: ["WisprAltCore"],
            path: "Probe",
            swiftSettings: [
                .swiftLanguageMode(.v5)
            ]
        ),
        .testTarget(
            name: "WisprAltCoreTests",
            dependencies: ["WisprAltCore"],
            path: "Tests/WisprAltCoreTests",
            swiftSettings: [
                .swiftLanguageMode(.v5)
            ]
        )
    ]
)
