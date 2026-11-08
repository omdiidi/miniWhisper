import Foundation
import Testing
import WisprAltCore

@Suite struct TranscriptsFolderDefaultTests {
    private let docs = URL(fileURLWithPath: "/Users/x/Documents", isDirectory: true)

    @Test func storedPath_wins_evenIfMeetingsExists() {
        let url = TranscriptsFolderDefault.resolve(stored: "/Volumes/Data/Notes", docs: docs, exists: { _ in true })
        #expect(url.path == "/Volumes/Data/Notes")
    }
    @Test func nothingStored_meetingsExists_pinsMeetings() {
        let url = TranscriptsFolderDefault.resolve(stored: nil, docs: docs, exists: { $0.lastPathComponent == "Meetings" })
        #expect(url.path == "/Users/x/Documents/WisprAlt/Meetings")
    }
    @Test func nothingStored_noMeetings_usesTranscripts() {
        let url = TranscriptsFolderDefault.resolve(stored: nil, docs: docs, exists: { _ in false })
        #expect(url.path == "/Users/x/Documents/WisprAlt/Transcripts")
    }
    @Test func emptyStored_treatedAsNothingStored() {
        let url = TranscriptsFolderDefault.resolve(stored: "", docs: docs, exists: { _ in false })
        #expect(url.path == "/Users/x/Documents/WisprAlt/Transcripts")
    }
}
