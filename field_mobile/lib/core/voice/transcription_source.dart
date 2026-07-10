/// Speech-to-text abstraction. The device implementation (the speech_to_text
/// plugin) lands at device-testing time; everything upstream depends only on
/// this interface so the capture → pre-fill flow is testable headless — the
/// same pattern as LocationSource.
library;

class TranscriptResult {
  const TranscriptResult({required this.text, this.confidence});

  final String text;

  /// ASR-reported confidence in [0,1], if the engine provides one.
  final double? confidence;
}

abstract class TranscriptionSource {
  /// Listen for a single utterance; null when unavailable, denied, or silent.
  Future<TranscriptResult?> listen();
}

class UnavailableTranscription implements TranscriptionSource {
  const UnavailableTranscription();

  @override
  Future<TranscriptResult?> listen() async => null;
}

class FakeTranscription implements TranscriptionSource {
  FakeTranscription(this.result);

  TranscriptResult? result;

  @override
  Future<TranscriptResult?> listen() async => result;
}
