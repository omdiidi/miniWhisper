import AVFoundation
import CoreAudio

enum MicEnumerator {
    struct InputDevice: Identifiable, Hashable {
        let uniqueID: String
        let name: String
        var id: String { uniqueID }
    }

    /// macOS 14+ device discovery for audio inputs. Project deployment target
    /// is macOS 15+ so we use the modern `.microphone` type (which subsumes
    /// `.builtInMicrophone` — that name was deprecated in macOS 14).
    /// `.externalUnknown` is iOS-only — do NOT add it.
    static func availableInputs() -> [InputDevice] {
        let deviceTypes: [AVCaptureDevice.DeviceType] = [.external, .microphone]
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: deviceTypes,
            mediaType: .audio,
            position: .unspecified
        )
        // Dedup by uniqueID — `.microphone` and `.external` can overlap for
        // some USB/Thunderbolt devices.
        var seen = Set<String>()
        return session.devices.compactMap { d in
            guard !seen.contains(d.uniqueID) else { return nil }
            seen.insert(d.uniqueID)
            return InputDevice(uniqueID: d.uniqueID, name: d.localizedName)
        }
    }

    /// Translate AVCaptureDevice.uniqueID (which IS the CoreAudio device UID)
    /// to an AudioDeviceID via kAudioHardwarePropertyTranslateUIDToDevice.
    /// Returns nil if the UID doesn't resolve.
    static func audioDeviceID(forUID uid: String) -> AudioDeviceID? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        var cfUID: CFString = uid as CFString
        var devID: AudioDeviceID = kAudioObjectUnknown
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let qualifierSize = UInt32(MemoryLayout<CFString?>.size)
        let status = withUnsafePointer(to: &cfUID) { uidPtr -> OSStatus in
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &addr,
                qualifierSize,
                uidPtr,
                &size,
                &devID
            )
        }
        guard status == noErr, devID != kAudioObjectUnknown else { return nil }
        return devID
    }

    /// Read system default input device's AudioDeviceID.
    static func systemDefaultInputDeviceID() -> AudioDeviceID? {
        var devID: AudioDeviceID = kAudioObjectUnknown
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &addr, 0, nil, &size, &devID
        )
        return status == noErr ? devID : nil
    }

    /// Get the UID for an AudioDeviceID, suitable for persistence.
    static func uid(forAudioDeviceID deviceID: AudioDeviceID) -> String? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        var uidRef: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &uidRef)
        guard status == noErr, let uid = uidRef?.takeRetainedValue() else { return nil }
        return uid as String
    }

    /// Set the system default input device. Returns true on success.
    /// **Side effect**: this changes the system-wide default for ALL apps.
    @discardableResult
    static func setSystemDefaultInputDevice(_ deviceID: AudioDeviceID) -> Bool {
        var devID = deviceID
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope:    kAudioObjectPropertyScopeGlobal,
            mElement:  kAudioObjectPropertyElementMain
        )
        let status = AudioObjectSetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &addr, 0, nil,
            UInt32(MemoryLayout<AudioDeviceID>.size),
            &devID
        )
        return status == noErr
    }

    /// Convenience: name of the current system default mic via AVFoundation
    /// (avoids the CoreAudio CFString out-param ownership trap).
    static func systemDefaultInputName() -> String? {
        AVCaptureDevice.default(for: .audio)?.localizedName
    }
}
