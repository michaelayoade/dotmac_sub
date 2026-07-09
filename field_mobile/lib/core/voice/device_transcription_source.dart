import 'dart:async';

import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;

import 'transcription_source.dart';

/// Real device speech-to-text via the `speech_to_text` plugin.
///
/// Returns a single final utterance; resolves null when the engine is
/// unavailable, microphone/speech permission is denied, or nothing intelligible
/// was heard. Callers fall back to manual entry on null, so a failure here never
/// blocks the capture → pre-fill flow.
class DeviceTranscriptionSource implements TranscriptionSource {
  DeviceTranscriptionSource({
    stt.SpeechToText? speech,
    this.listenFor = const Duration(seconds: 30),
    this.pauseFor = const Duration(seconds: 3),
    this.localeId,
  }) : _speech = speech ?? stt.SpeechToText();

  final stt.SpeechToText _speech;
  final Duration listenFor;
  final Duration pauseFor;
  final String? localeId;
  bool _initialized = false;

  Future<bool> _ensureInitialized() async {
    if (_initialized) return true;
    // initialize() prompts for mic/speech permission and returns false when
    // denied or unsupported. Retry on a later call if it failed (permission
    // may be granted in the meantime).
    _initialized = await _speech.initialize(onError: (_) {}, onStatus: (_) {});
    return _initialized;
  }

  @override
  Future<TranscriptResult?> listen() async {
    if (!await _ensureInitialized()) return null;

    final completer = Completer<TranscriptResult?>();
    void finish(TranscriptResult? result) {
      if (!completer.isCompleted) completer.complete(result);
    }

    void onResult(SpeechRecognitionResult result) {
      if (!result.finalResult) return;
      final text = result.recognizedWords.trim();
      finish(
        text.isEmpty
            ? null
            : TranscriptResult(
                text: text,
                confidence: result.hasConfidenceRating ? result.confidence : null,
              ),
      );
    }

    try {
      await _speech.listen(
        onResult: onResult,
        listenOptions: stt.SpeechListenOptions(
          partialResults: false,
          listenFor: listenFor,
          pauseFor: pauseFor,
          localeId: localeId,
        ),
      );
    } catch (_) {
      return null;
    }

    // Safety net: if no final result ever arrives, stop and resolve null so the
    // UI never hangs waiting on the mic.
    return completer.future.timeout(
      listenFor + const Duration(seconds: 2),
      onTimeout: () async {
        await _speech.stop();
        return null;
      },
    );
  }
}
