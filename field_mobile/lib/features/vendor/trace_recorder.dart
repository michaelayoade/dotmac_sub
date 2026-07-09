import 'dart:math' as math;

import '../../core/location/location_source.dart';

/// Walk-recorded as-built route: accumulates GPS points into a GeoJSON
/// LineString with live distance. Pure logic — the screen feeds it points
/// from the location abstraction.
class TraceRecorder {
  final List<GeoPoint> points = [];
  bool recording = false;

  void start() {
    points.clear();
    recording = true;
  }

  void addPoint(GeoPoint point) {
    if (!recording) return;
    // Drop GPS jitter: ignore points < 2 m from the previous fix.
    if (points.isNotEmpty && _haversineMeters(points.last, point) < 2) return;
    points.add(point);
  }

  void stop() => recording = false;

  double get distanceMeters {
    var total = 0.0;
    for (var i = 0; i + 1 < points.length; i++) {
      total += _haversineMeters(points[i], points[i + 1]);
    }
    return total;
  }

  bool get hasUsableTrace => points.length >= 2;

  Map<String, dynamic> toGeoJson() => {
    'type': 'LineString',
    'coordinates': [
      for (final point in points) [point.longitude, point.latitude],
    ],
  };
}

double _haversineMeters(GeoPoint a, GeoPoint b) {
  const earthRadius = 6371000.0;
  final dLat = _rad(b.latitude - a.latitude);
  final dLng = _rad(b.longitude - a.longitude);
  final h =
      math.pow(math.sin(dLat / 2), 2) +
      math.cos(_rad(a.latitude)) *
          math.cos(_rad(b.latitude)) *
          math.pow(math.sin(dLng / 2), 2);
  return 2 * earthRadius * math.asin(math.sqrt(h.toDouble()));
}

double _rad(double degrees) => degrees * math.pi / 180;
