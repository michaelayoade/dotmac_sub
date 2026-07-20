import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/voice/device_transcription_source.dart';
import '../../core/voice/transcription_source.dart';
import '../auth/auth_state.dart';

/// Structured field data extracted from a spoken note (mirrors the backend
/// VoiceExtractResponse). `requiresReview` is the quality gate (task #50): when
/// true the UI must make the tech confirm every field before saving.
class FieldExtractionResult {
  const FieldExtractionResult({
    required this.transcript,
    this.workStatus,
    this.equipmentSerial,
    this.signalReadings = const {},
    this.materialsUsed = const [],
    this.notes = '',
    this.confidence,
    this.requiresReview = true,
    this.reviewReasons = const [],
  });

  final String transcript;
  final String? workStatus;
  final String? equipmentSerial;
  final Map<String, String> signalReadings;
  final List<Map<String, String?>> materialsUsed;
  final String notes;
  final double? confidence;
  final bool requiresReview;
  final List<String> reviewReasons;

  factory FieldExtractionResult.fromJson(
    Map<String, dynamic> json, {
    required String transcript,
  }) {
    final readings = <String, String>{};
    final rawReadings = json['signal_readings'];
    if (rawReadings is Map) {
      rawReadings.forEach((k, v) => readings[k.toString()] = v.toString());
    }
    final materials = <Map<String, String?>>[];
    final rawMaterials = json['materials_used'];
    if (rawMaterials is List) {
      for (final item in rawMaterials) {
        if (item is Map) {
          materials.add({
            'name': item['name']?.toString(),
            'quantity': item['quantity']?.toString(),
          });
        }
      }
    }
    return FieldExtractionResult(
      transcript: transcript,
      workStatus: json['work_status'] as String?,
      equipmentSerial: json['equipment_serial'] as String?,
      signalReadings: readings,
      materialsUsed: materials,
      notes: json['notes'] as String? ?? '',
      confidence: (json['confidence'] as num?)?.toDouble(),
      requiresReview: json['requires_review'] as bool? ?? true,
      reviewReasons: ((json['review_reasons'] as List?) ?? const [])
          .map((e) => e.toString())
          .toList(),
    );
  }
}

/// Posts a transcript to the extraction endpoint; returns the parsed JSON body.
typedef ExtractPoster =
    Future<Map<String, dynamic>> Function(
      String transcript, {
      String? context,
      double? asrConfidence,
    });

/// Orchestrates: capture speech → server extraction → pre-fill model the UI
/// presents for confirmation. Fails safe — anything unavailable returns null so
/// the tech just fills the form manually.
class VoiceCaptureController {
  VoiceCaptureController({required this.transcription, required this.poster});

  final TranscriptionSource transcription;
  final ExtractPoster poster;

  /// Returns a pre-fill result to confirm, or null when there's nothing usable
  /// (no transcript captured, or the server call failed).
  Future<FieldExtractionResult?> capture({String? context}) async {
    final heard = await transcription.listen();
    final text = (heard?.text ?? '').trim();
    if (text.isEmpty) return null;
    try {
      final body = await poster(
        text,
        context: context,
        asrConfidence: heard?.confidence,
      );
      return FieldExtractionResult.fromJson(body, transcript: text);
    } catch (_) {
      // Server/network failure: hand back the raw transcript so the tech can
      // still use it, flagged for review.
      return FieldExtractionResult(
        transcript: text,
        notes: text,
        requiresReview: true,
        reviewReasons: const ['extraction_unavailable'],
      );
    }
  }
}

final voiceCaptureControllerProvider = Provider<VoiceCaptureController>((ref) {
  final api = ref.watch(apiClientProvider);
  return VoiceCaptureController(
    transcription: DeviceTranscriptionSource(), // real device speech-to-text
    poster: (transcript, {context, asrConfidence}) async {
      final response = await api.dio.post(
        '/api/v1/field/voice/extract',
        data: {
          'transcript': transcript,
          'context': ?context,
          'asr_confidence': ?asrConfidence,
        },
      );
      return (response.data as Map).cast<String, dynamic>();
    },
  );
});
