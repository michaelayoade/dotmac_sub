import 'package:dotmac_field/features/vendor/trace_recorder.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('trace recorder', () {
    test('accumulates points, filters jitter, computes distance', () {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.4281, longitude: 3.4216));
      recorder.addPoint((
        latitude: 6.4281,
        longitude: 3.4216,
      )); // jitter: dropped
      recorder.addPoint((latitude: 6.4290, longitude: 3.4216)); // ~100 m north
      recorder.stop();

      expect(recorder.points.length, 2);
      expect(recorder.distanceMeters, closeTo(100, 5));
      expect(recorder.hasUsableTrace, isTrue);
    });

    test('geojson is a LineString of lng,lat pairs', () {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.0, longitude: 3.0));
      recorder.addPoint((latitude: 6.001, longitude: 3.001));
      final geojson = recorder.toGeoJson();

      expect(geojson['type'], 'LineString');
      expect(geojson['coordinates'], [
        [3.0, 6.0],
        [3.001, 6.001],
      ]);
    });

    test('single point is not a usable trace', () {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.0, longitude: 3.0));
      expect(recorder.hasUsableTrace, isFalse);
    });
  });
}
