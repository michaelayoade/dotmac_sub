import 'package:dotmac_field/core/voice/transcription_source.dart';
import 'package:dotmac_field/features/voice/voice_extraction.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('FieldExtractionResult.fromJson', () {
    test('parses structured fields', () {
      final result = FieldExtractionResult.fromJson({
        'work_status': 'completed',
        'equipment_serial': 'HG8546',
        'signal_readings': {'Downstream': '-21 dB'},
        'materials_used': [
          {'name': 'drop cable', 'quantity': '40 m'},
        ],
        'notes': 'customer happy',
        'confidence': 0.82,
        'requires_review': false,
        'review_reasons': [],
      }, transcript: 'installed the ont');
      expect(result.workStatus, 'completed');
      expect(result.equipmentSerial, 'HG8546');
      expect(result.signalReadings['Downstream'], '-21 dB');
      expect(result.materialsUsed.first['name'], 'drop cable');
      expect(result.requiresReview, isFalse);
    });

    test('defaults requires_review to true when absent', () {
      final result = FieldExtractionResult.fromJson({
        'work_status': 'completed',
      }, transcript: 't');
      expect(result.requiresReview, isTrue);
    });
  });

  group('VoiceCaptureController', () {
    test('returns null when nothing is heard', () async {
      final controller = VoiceCaptureController(
        transcription: FakeTranscription(null),
        poster: (_, {context, asrConfidence}) async => {},
      );
      expect(await controller.capture(), isNull);
    });

    test('returns null on empty/whitespace transcript', () async {
      final controller = VoiceCaptureController(
        transcription: FakeTranscription(const TranscriptResult(text: '   ')),
        poster: (_, {context, asrConfidence}) async => {},
      );
      expect(await controller.capture(), isNull);
    });

    test('captures, extracts, and forwards asr confidence', () async {
      double? sentConfidence;
      String? sentContext;
      final controller = VoiceCaptureController(
        transcription: FakeTranscription(
          const TranscriptResult(text: 'installed the ont', confidence: 0.9),
        ),
        poster: (transcript, {context, asrConfidence}) async {
          sentConfidence = asrConfidence;
          sentContext = context;
          return {
            'work_status': 'completed',
            'requires_review': false,
            'confidence': 0.88,
          };
        },
      );
      final result = await controller.capture(context: 'install');
      expect(result, isNotNull);
      expect(result!.workStatus, 'completed');
      expect(result.requiresReview, isFalse);
      expect(sentConfidence, 0.9);
      expect(sentContext, 'install');
      expect(result.transcript, 'installed the ont');
    });

    test(
      'falls back to raw transcript flagged for review on server failure',
      () async {
        final controller = VoiceCaptureController(
          transcription: FakeTranscription(
            const TranscriptResult(text: 'installed the ont'),
          ),
          poster: (_, {context, asrConfidence}) async =>
              throw Exception('network down'),
        );
        final result = await controller.capture();
        expect(result, isNotNull);
        expect(result!.requiresReview, isTrue);
        expect(result.reviewReasons, contains('extraction_unavailable'));
        expect(result.notes, 'installed the ont');
      },
    );
  });
}
