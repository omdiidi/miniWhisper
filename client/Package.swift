// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "WisprAlt",
    platforms: [
        .macOS(.v14)
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
            swiftSettings: [
                .unsafeFlags(["-strict-concurrency=complete"])
            ]
        )
    ]
)
