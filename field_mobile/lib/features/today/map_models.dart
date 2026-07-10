import 'dart:convert';

import '../../core/location/map_coordinates.dart';
import '../jobs/job_models.dart';

class JobPin {
  const JobPin({
    required this.id,
    required this.title,
    required this.status,
    required this.latitude,
    required this.longitude,
    this.addressText,
  });

  final String id;
  final String title;
  final String status;
  final double latitude;
  final double longitude;
  final String? addressText;

  bool get hasValidCoordinates => isValidMapCoordinate(latitude, longitude);
}

class MapAsset {
  const MapAsset({
    required this.id,
    required this.type,
    required this.title,
    this.subtitle,
    required this.latitude,
    required this.longitude,
    this.status,
    this.updatedAt,
  });

  final String id;
  final String type;
  final String title;
  final String? subtitle;
  final double latitude;
  final double longitude;
  final String? status;
  final DateTime? updatedAt;

  bool get hasValidCoordinates => isValidMapCoordinate(latitude, longitude);

  factory MapAsset.fromJson(Map<String, dynamic> json) => MapAsset(
    id: json['id'] as String,
    type: json['type'] as String,
    title: json['title'] as String,
    subtitle: json['subtitle'] as String?,
    latitude: (json['latitude'] as num).toDouble(),
    longitude: (json['longitude'] as num).toDouble(),
    status: json['status'] as String?,
    updatedAt: json['updated_at'] != null
        ? DateTime.parse(json['updated_at'] as String).toUtc()
        : null,
  );
}

class MapPlaceSearchResult {
  const MapPlaceSearchResult({
    required this.kind,
    required this.id,
    required this.title,
    required this.latitude,
    required this.longitude,
    this.assetType,
    this.subtitle,
    this.status,
    this.addressText,
  });

  final String kind;
  final String id;
  final String title;
  final double latitude;
  final double longitude;
  final String? assetType;
  final String? subtitle;
  final String? status;
  final String? addressText;

  bool get hasValidCoordinates => isValidMapCoordinate(latitude, longitude);

  factory MapPlaceSearchResult.fromJson(Map<String, dynamic> json) =>
      MapPlaceSearchResult(
        kind: json['kind'] as String,
        id: json['id'].toString(),
        title: json['title'] as String,
        latitude: (json['latitude'] as num).toDouble(),
        longitude: (json['longitude'] as num).toDouble(),
        assetType: json['asset_type'] as String?,
        subtitle: json['subtitle'] as String?,
        status: json['status'] as String?,
        addressText: json['address_text'] as String?,
      );
}

const mapAssetTypeLabels = {
  'olt': 'OLT',
  'fdh': 'FDH',
  'fiber_access_point': 'FAP',
  'splice_closure': 'Closure',
  'wireless_mast': 'Mast',
  'service_building': 'Building',
};

const defaultMapAssetTypes = {
  'olt',
  'fdh',
  'fiber_access_point',
  'splice_closure',
  'wireless_mast',
};

/// Build map pins from job summaries + cached detail JSON (keyed by job id).
/// Jobs without resolved coordinates are skipped — the map shows what it
/// knows; the list view remains the complete source of truth.
List<JobPin> buildJobPins(
  List<JobSummary> jobs,
  Map<String, String?> detailJsonById,
) {
  final pins = <JobPin>[];
  for (final job in jobs) {
    final raw = detailJsonById[job.id];
    if (raw == null) continue;
    final detail = (jsonDecode(raw) as Map).cast<String, dynamic>();
    final location = (detail['location'] as Map?)?.cast<String, dynamic>();
    final lat = (location?['latitude'] as num?)?.toDouble();
    final lng = (location?['longitude'] as num?)?.toDouble();
    if (lat == null || lng == null) continue;
    if (!isValidMapCoordinate(lat, lng)) continue;
    pins.add(
      JobPin(
        id: job.id,
        title: job.title,
        status: job.status,
        latitude: lat,
        longitude: lng,
        addressText:
            (location?['address_text'] as String?) ??
            ((detail['customer'] as Map?)?['address_text'] as String?),
      ),
    );
  }
  return pins;
}
