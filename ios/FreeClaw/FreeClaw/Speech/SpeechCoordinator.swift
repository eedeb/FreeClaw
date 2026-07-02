import Foundation
import AVFoundation
import Speech

/// Fully on-device push-to-talk: Apple's Speech framework transcribes the
/// mic while held, AVSpeechSynthesizer reads the agent's reply back once it
/// streams in. No audio or transcript ever leaves the device — the server
/// only ever sees the same plain-text POST /chat requests it always has.
@MainActor
final class SpeechCoordinator: NSObject, ObservableObject {
    enum PermissionState {
        case unknown, granted, denied
    }

    @Published private(set) var isRecording = false
    @Published private(set) var isSpeaking = false
    @Published private(set) var liveTranscript = ""
    @Published private(set) var permissionState: PermissionState = .unknown
    @Published var errorMessage: String?

    private let speechRecognizer = SFSpeechRecognizer()
    private let synthesizer = AVSpeechSynthesizer()
    private let audioEngine = AVAudioEngine()

    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var finalTranscriptContinuation: CheckedContinuation<String, Never>?

    /// Not-yet-spoken tail of the current reply. Flushed sentence-by-
    /// sentence as tokens stream in so playback starts well before the
    /// full reply has arrived, rather than waiting for "done".
    private var pendingSpeechBuffer = ""

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    // MARK: - Permissions

    /// Requests speech-recognition and microphone access together, since
    /// voice mode needs both. Safe to call repeatedly — iOS only prompts
    /// once and just replays the stored decision after that.
    func requestPermissions() async -> Bool {
        let speechStatus = await withCheckedContinuation { (continuation: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
        guard speechStatus == .authorized else {
            permissionState = .denied
            return false
        }

        let micGranted = await requestMicrophonePermission()
        permissionState = micGranted ? .granted : .denied
        return micGranted
    }

    private func requestMicrophonePermission() async -> Bool {
        if #available(iOS 17.0, *) {
            return await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
        } else {
            return await withCheckedContinuation { continuation in
                AVAudioSession.sharedInstance().requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
        }
    }

    // MARK: - Recording (speech-to-text)

    func startRecording() {
        guard !isRecording else { return }
        errorMessage = nil
        stopSpeaking()

        guard let speechRecognizer, speechRecognizer.isAvailable else {
            errorMessage = "Speech recognition isn't available right now."
            return
        }

        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .default, options: [.duckOthers, .defaultToSpeaker, .allowBluetooth])
            try session.setActive(true, options: .notifyOthersOnDeactivation)

            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            request.taskHint = .dictation
            // Prefer on-device recognition when the device/locale supports
            // it, so nothing gets sent off-device to transcribe. Where it
            // isn't supported, iOS transparently falls back to its own
            // server-based recognizer rather than failing outright — still
            // never touches the FreeClaw server either way.
            if speechRecognizer.supportsOnDeviceRecognition {
                request.requiresOnDeviceRecognition = true
            }
            recognitionRequest = request

            let inputNode = audioEngine.inputNode
            let format = inputNode.outputFormat(forBus: 0)
            inputNode.removeTap(onBus: 0)
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
                self?.recognitionRequest?.append(buffer)
            }

            audioEngine.prepare()
            try audioEngine.start()

            liveTranscript = ""
            isRecording = true

            recognitionTask = speechRecognizer.recognitionTask(with: request) { [weak self] result, error in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if let result {
                        self.liveTranscript = result.bestTranscription.formattedString
                        if result.isFinal {
                            self.resumeFinalTranscript(with: self.liveTranscript)
                        }
                    }
                    if error != nil {
                        self.resumeFinalTranscript(with: self.liveTranscript)
                    }
                }
            }
        } catch {
            errorMessage = "Couldn't start listening: \(error.localizedDescription)"
            teardownRecording()
        }
    }

    /// Stops capturing audio and waits briefly for the recognizer's final
    /// transcript (falling back to the last partial result if it never
    /// reports one). Returns nil if nothing usable was heard.
    func stopRecordingAndGetTranscript() async -> String? {
        guard isRecording else { return nil }
        let transcriptSoFar = liveTranscript
        recognitionRequest?.endAudio()

        let finalText = await withCheckedContinuation { (continuation: CheckedContinuation<String, Never>) in
            finalTranscriptContinuation = continuation
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                self?.resumeFinalTranscript(with: transcriptSoFar)
            }
        }

        teardownRecording()
        let trimmed = finalText.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    func cancelRecording() {
        teardownRecording()
        liveTranscript = ""
    }

    private func resumeFinalTranscript(with text: String) {
        guard let continuation = finalTranscriptContinuation else { return }
        finalTranscriptContinuation = nil
        continuation.resume(returning: text)
    }

    private func teardownRecording() {
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest = nil
        recognitionTask?.cancel()
        recognitionTask = nil
        isRecording = false
    }

    /// Fully releases the mic/audio session — call when voice mode turns
    /// off or the chat screen disappears, so other apps get audio focus
    /// back instead of FreeClaw holding it indefinitely.
    func deactivateSession() {
        stopSpeaking()
        cancelRecording()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    // MARK: - Speaking (text-to-speech)

    /// Feed a newly-streamed chunk of the agent's reply in; complete
    /// sentences are spoken as soon as they're recognized so playback
    /// starts well before the full reply has finished streaming.
    func appendReplyToken(_ piece: String) {
        pendingSpeechBuffer += piece
        flushCompleteSentences()
    }

    /// Called when a tool call starts. Any not-yet-spoken text queued so
    /// far belongs to a message the chat UI itself discards (see
    /// ChatView's handling of the "tool_call" event, which drops the live
    /// bubble) — so drop it here too rather than speak something that
    /// never ends up on screen. Tool calls themselves are never spoken;
    /// they only ever reach the UI as separate events, never as reply
    /// tokens, so there's nothing else to filter.
    func discardPendingReply() {
        pendingSpeechBuffer = ""
    }

    /// Called at the end of a reply (or on error) to speak whatever's left
    /// in the buffer, even if it never hit sentence-ending punctuation.
    func finishReply() {
        let remainder = pendingSpeechBuffer.trimmingCharacters(in: .whitespacesAndNewlines)
        pendingSpeechBuffer = ""
        if !remainder.isEmpty {
            speak(remainder)
        }
    }

    func stopSpeaking() {
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        pendingSpeechBuffer = ""
        isSpeaking = false
    }

    private func flushCompleteSentences() {
        while let range = pendingSpeechBuffer.range(of: #"[.!?]\s"#, options: .regularExpression) {
            let sentence = String(pendingSpeechBuffer[..<range.upperBound])
            pendingSpeechBuffer.removeSubrange(pendingSpeechBuffer.startIndex..<range.upperBound)
            speak(sentence)
        }
    }

    private func speak(_ rawText: String) {
        let text = SpeechCoordinator.cleanedForSpeech(rawText)
        guard !text.isEmpty else { return }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: nil) // device's default system voice
        synthesizer.speak(utterance)
    }

    /// Strips markdown syntax the model tends to produce (code fences,
    /// inline code, bold/italic/heading markers, bullet dashes, link
    /// brackets) so AVSpeechSynthesizer doesn't read punctuation
    /// characters out loud — the server isn't involved in this cleanup,
    /// it all happens here right before an utterance is queued.
    static func cleanedForSpeech(_ text: String) -> String {
        var result = text
        result = result.replacingOccurrences(of: #"```[\s\S]*?```"#, with: "", options: .regularExpression)
        result = result.replacingOccurrences(of: "`", with: "")
        result = result.replacingOccurrences(of: #"\[([^\]]+)\]\([^)]+\)"#, with: "$1", options: .regularExpression)
        result = result.replacingOccurrences(of: #"[*_#>]+"#, with: "", options: .regularExpression)
        result = result.replacingOccurrences(of: #"^\s*[-•]\s+"#, with: "", options: [.regularExpression, .anchored])
        return result.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

extension SpeechCoordinator: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        Task { @MainActor in self.isSpeaking = true }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in
            if !synthesizer.isSpeaking { self.isSpeaking = false }
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in self.isSpeaking = false }
    }
}
