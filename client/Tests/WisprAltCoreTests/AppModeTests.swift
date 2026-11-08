import Foundation
import Testing
import WisprAltCore

@Suite struct AppModeTests {
    private let model = OpenAIModels.resolve(id: nil)
    private let endpoint = ServerEndpoint(baseURL: URL(string: "https://mini.example.com")!, apiKey: "k")

    private func state(mode: TranscriptionMode = .local, config: Bool = true, saved: Bool = true,
                       poller: Bool = false, online: Bool = false, me: Bool = false) -> LocalJobState {
        LocalJobState(mode: mode, hasServerConfig: config, hasSavedJobID: saved,
                      pollerRunning: poller, onlineJobRunning: online, meFetchedThisLaunch: me)
    }

    // MARK: Route

    @Test func defaultMode_isOnline() {
        #expect(TranscriptionMode.default == .online)
    }

    @Test func onlineWithKey_routesOpenAIFile_noStreamEndpoint() {
        let s = DictationSnapshot(mode: .online, model: model, keyPresent: true, endpoint: endpoint)
        #expect(s.kind == .openAIFile)
        #expect(s.streamEndpoint == nil)
        #expect(s.endpoint == nil)
        #expect(s.model == model)
    }

    @Test func onlineLiveModel_routesRealtime_noStreamEndpoint() {
        let live = OpenAIModels.model(id: "gpt-live-transcribe")!
        #expect(RouteKind.decide(mode: .online, modelKind: .live, keyPresent: true) == .openAIRealtime)
        #expect(RouteKind.decide(mode: .online, modelKind: .live, keyPresent: false) == .needKey)
        #expect(RouteKind.decide(mode: .local, modelKind: .live, keyPresent: true) == .local)
        let s = DictationSnapshot(mode: .online, model: live, keyPresent: true, endpoint: endpoint)
        #expect(s.kind == .openAIRealtime)
        #expect(s.streamEndpoint == nil)
        #expect(s.endpoint == nil)
        #expect(s.capSeconds == 600)
        let whisper = DictationSnapshot(mode: .online, model: OpenAIModels.model(id: "gpt-realtime-whisper")!,
                                        keyPresent: true, endpoint: nil)
        #expect(whisper.capSeconds == 420)
    }

    @Test func onlineWithoutKey_needsKey() {
        #expect(RouteKind.decide(mode: .online, modelKind: .file, keyPresent: false) == .needKey)
        let s = DictationSnapshot(mode: .online, model: model, keyPresent: false, endpoint: endpoint)
        #expect(s.kind == .needKey)
        #expect(s.streamEndpoint == nil)
    }

    @Test func local_routesLocal_withEndpoint() {
        let s = DictationSnapshot(mode: .local, model: model, keyPresent: false, endpoint: endpoint)
        #expect(s.kind == .local)
        #expect(s.streamEndpoint == endpoint)
        #expect(s.model == nil)
        #expect(s.capSeconds == 600)
    }

    @Test func snapshotId_isLowercaseUUID() {
        let s = DictationSnapshot(mode: .online, model: model, keyPresent: true, endpoint: nil)
        #expect(UUID(uuidString: s.id) != nil)
        #expect(s.id == s.id.lowercased())
    }

    // MARK: LaunchActions

    @Test func launchOnline_doesNothing() {
        #expect(LaunchActions.decide(.launch, state(mode: .online)) == LaunchActions())
    }

    @Test func launchLocal_withConfigAndSavedJob_resumesAndFetchesMe() {
        #expect(LaunchActions.decide(.launch, state()) == LaunchActions(resumeLocalJob: true, fetchMe: true))
    }

    @Test func launchLocal_withoutConfig_doesNothing() {
        #expect(LaunchActions.decide(.launch, state(config: false)) == LaunchActions())
    }

    @Test func switchToOnline_withPoller_cancelsLocalPollOnly() {
        let a = LaunchActions.decide(.switched(to: .online), state(mode: .online, poller: true))
        #expect(a == LaunchActions(cancelLocalPoll: true))
    }

    @Test func switchToLocal_savedJobIdle_resumes_meOnlyIfNotFetched() {
        #expect(LaunchActions.decide(.switched(to: .local), state()) == LaunchActions(resumeLocalJob: true, fetchMe: true))
        #expect(LaunchActions.decide(.switched(to: .local), state(me: true)) == LaunchActions(resumeLocalJob: true))
    }

    @Test func switchToLocal_alreadyPolling_noResume() {
        #expect(!LaunchActions.decide(.switched(to: .local), state(poller: true)).resumeLocalJob)
    }

    @Test func switchToLocal_whileOnlineJobRuns_noResume_noCancel() {
        let a = LaunchActions.decide(.switched(to: .local), state(online: true))
        #expect(!a.resumeLocalJob)
        #expect(!a.cancelLocalPoll)
    }

    @Test func switchToOnline_whileOnlineJobRuns_neverCancelsIt() {
        // No field of LaunchActions can reach an Online job; only the Local poll.
        let a = LaunchActions.decide(.switched(to: .online), state(mode: .online, online: true))
        #expect(a == LaunchActions())
    }

    // MARK: Online file job end (Task 9)

    @Test func onlineJobFinished_thenSwitchToLocal_resumes() {
        #expect(LaunchActions.decide(.switched(to: .local), state(online: false)).resumeLocalJob)
    }

    @Test func switchToLocalDuringOnlineJob_thenJobEnds_resumesWithoutMe() {
        // The switch itself does nothing while the Online job runs …
        #expect(!LaunchActions.decide(.switched(to: .local), state(online: true)).resumeLocalJob)
        // … and its end (task handle already cleared) resumes the saved Local job.
        let a = LaunchActions.decide(.onlineJobEnded, state(online: false))
        #expect(a.resumeLocalJob)
        #expect(!a.fetchMe)
        #expect(!a.cancelLocalPoll)
    }

    @Test func onlineJobEnded_inOnline_doesNothing() {
        #expect(LaunchActions.decide(.onlineJobEnded, state(mode: .online)) == LaunchActions())
    }

    @Test func onlineJobEnded_pollerAlreadyRunning_noResume() {
        #expect(!LaunchActions.decide(.onlineJobEnded, state(poller: true)).resumeLocalJob)
    }

    @Test func meAlreadyFetched_noFetchMe() {
        #expect(!LaunchActions.decide(.launch, state(me: true)).fetchMe)
    }

    // MARK: PollFence

    @Test func pollFence_table() {
        #expect(!PollFence.mayWrite(isCancelled: true, captured: 1, current: 1))
        #expect(!PollFence.mayWrite(isCancelled: false, captured: 1, current: 2))
        #expect(PollFence.mayWrite(isCancelled: false, captured: 2, current: 2))
    }

    // MARK: Recording across a switch

    @Test func recordingAcrossSwitch_keepsItsRoute_andStaysDictating() {
        let local = DictationSnapshot(mode: .local, model: model, keyPresent: false, endpoint: endpoint)
        let toOnline = LaunchActions.decide(.switched(to: .online), state(mode: .online))
        #expect(local.kind == .local)
        #expect(local.streamEndpoint == endpoint)
        #expect(toOnline == LaunchActions())
        #expect(ActivityState.icon(dictating: true, local: .none, onlineJob: false, doneFlash: false) == .dictating)

        let online = DictationSnapshot(mode: .online, model: model, keyPresent: true, endpoint: nil)
        let toLocal = LaunchActions.decide(.switched(to: .local), state(saved: false))
        #expect(online.kind == .openAIFile)
        #expect(online.streamEndpoint == nil)
        #expect(toLocal == LaunchActions(fetchMe: true))
        #expect(ActivityState.icon(dictating: true, local: .polling, onlineJob: false, doneFlash: false) == .dictating)
    }

    // MARK: ActivityState

    @Test func activityState_precedence() {
        for phase in [LocalUploadPhase.none, .converting, .uploading, .polling] {
            #expect(ActivityState.icon(dictating: true, local: phase, onlineJob: true, doneFlash: true) == .dictating)
        }
        #expect(ActivityState.icon(dictating: false, local: .converting, onlineJob: true, doneFlash: true) == .converting)
        #expect(ActivityState.icon(dictating: false, local: .uploading, onlineJob: true, doneFlash: true) == .uploading)
        #expect(ActivityState.icon(dictating: false, local: .polling, onlineJob: false, doneFlash: true) == .processing)
        #expect(ActivityState.icon(dictating: false, local: .none, onlineJob: true, doneFlash: true) == .processing)
        #expect(ActivityState.icon(dictating: false, local: .none, onlineJob: false, doneFlash: true) == .done)
        #expect(ActivityState.icon(dictating: false, local: .none, onlineJob: false, doneFlash: false) == .idle)
    }

    @Test func locksModePicker_onlyBeforeAJobID() {
        #expect(LocalUploadPhase.converting.locksModePicker)
        #expect(LocalUploadPhase.uploading.locksModePicker)
        #expect(!LocalUploadPhase.polling.locksModePicker)
        #expect(!LocalUploadPhase.none.locksModePicker)
    }

    // MARK: Race: one effective poller

    /// Poller A (gen 1) → switch Online → switch Local → poller B (gen 3) →
    /// A's tail runs. Only B may write; the job id survives A's tail.
    @Test func race_quickSwitchBack_leavesExactlyOneEffectivePoller() {
        var gen = 0
        var poller: Int? = nil          // generation of the task in `activeJobTask`
        var activeJobID: String? = "job-1"

        gen += 1; poller = gen           // A starts
        let a = gen

        var actions = LaunchActions.decide(.switched(to: .online), state(mode: .online, poller: poller != nil))
        #expect(actions.cancelLocalPoll)
        gen += 1; poller = nil           // cancel: bump, drop the handle (A's task is cancelled)

        actions = LaunchActions.decide(.switched(to: .local), state(poller: poller != nil, me: true))
        #expect(actions.resumeLocalJob)
        gen += 1; poller = gen           // B starts
        let b = gen
        #expect(b == 3)

        // A's tail (it was cancelled and its generation is old) tries to clear the job.
        if PollFence.mayWrite(isCancelled: true, captured: a, current: gen) { activeJobID = nil; poller = nil }
        if PollFence.mayWrite(isCancelled: false, captured: a, current: gen) { activeJobID = nil; poller = nil }

        #expect(poller == b)
        #expect(activeJobID == "job-1")
        #expect(PollFence.mayWrite(isCancelled: false, captured: b, current: gen))
        #expect(!LaunchActions.decide(.switched(to: .local), state(poller: poller != nil)).resumeLocalJob)
    }
}
