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
        .executableTarget(
            name: "WisprAlt",
            dependencies: [
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
        )
    ]
)
