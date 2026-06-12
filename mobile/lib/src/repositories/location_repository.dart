import 'package:dio/dio.dart';

import '../core/http.dart';
import '../models/service_location.dart';

/// Wraps the self-scoped service-location endpoints (app/api/me.py,
/// /me/location*): pin validation with admin-reviewed corrections.
class LocationRepository {
  LocationRepository(this.dio);

  final Dio dio;

  /// GET /me/location
  Future<ServiceLocation> location() async {
    final data = await guard(() => dio.get('/me/location'));
    return ServiceLocation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/location-requests — submit a pin correction for review.
  Future<LocationRequest> submitCorrection({
    required double latitude,
    required double longitude,
    String? note,
  }) async {
    final data = await guard(
      () => dio.post(
        '/me/location-requests',
        data: {
          'latitude': latitude,
          'longitude': longitude,
          if (note != null && note.isNotEmpty) 'note': note,
        },
      ),
    );
    return LocationRequest.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/location-requests/{id}/cancel
  Future<LocationRequest> cancelRequest(String requestId) async {
    final data = await guard(
      () => dio.post('/me/location-requests/$requestId/cancel'),
    );
    return LocationRequest.fromJson(data as Map<String, dynamic>);
  }

  /// GET /me/geocode/reverse — nearest known address for coordinates, or
  /// null when the point is unknown.
  Future<String?> reverseGeocode(double latitude, double longitude) async {
    final data = await guard(
      () => dio.get(
        '/me/geocode/reverse',
        queryParameters: {'lat': latitude, 'lon': longitude},
      ),
    );
    return (data as Map<String, dynamic>)['display_name'] as String?;
  }
}
