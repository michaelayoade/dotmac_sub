import 'package:latlong2/latlong.dart';

import '../../app/status_presentation.dart';
import '../../core/location/map_coordinates.dart';

const workOrderEvidenceContexts = {
  'current_source',
  'superseded_source',
  'current_and_superseded_source',
};

const workOrderEvidenceGeometryStates = {
  'exact_geojson',
  'source_geometry_unrenderable',
};

class WorkOrderEvidenceMapSnapshot {
  const WorkOrderEvidenceMapSnapshot({
    required this.reportSha256,
    required this.sourceOverlaySha256,
    required this.worklistReportSha256,
    required this.observationEvidenceSha256,
    required this.workOrderId,
    required this.workOrderPublicId,
    required this.observationCount,
    required this.currentSourceObservationCount,
    required this.supersededSourceObservationCount,
    required this.features,
    required this.fromCache,
    this.cachedAt,
  });

  final String reportSha256;
  final String sourceOverlaySha256;
  final String worklistReportSha256;
  final String observationEvidenceSha256;
  final String workOrderId;
  final String workOrderPublicId;
  final int observationCount;
  final int currentSourceObservationCount;
  final int supersededSourceObservationCount;
  final List<WorkOrderEvidenceFeature> features;
  final bool fromCache;
  final DateTime? cachedAt;

  String get cacheKey => '$workOrderPublicId:$reportSha256';
  int get currentFeatureCount => features
      .where(
        (feature) =>
            feature.context == 'current_source' ||
            feature.context == 'current_and_superseded_source',
      )
      .length;
  int get supersededFeatureCount => features
      .where(
        (feature) =>
            feature.context == 'superseded_source' ||
            feature.context == 'current_and_superseded_source',
      )
      .length;
  int get unrenderableFeatureCount =>
      features.where((feature) => !feature.isRenderable).length;
  List<LatLng> get allRenderedPoints => [
    for (final feature in features) ...feature.renderedPoints,
  ];

  factory WorkOrderEvidenceMapSnapshot.fromJson(
    Map<String, dynamic> json, {
    required String requestedWorkOrderPublicId,
    bool fromCache = false,
    DateTime? cachedAt,
  }) {
    final workOrderPublicId = _requiredString(json, 'work_order_public_id');
    if (workOrderPublicId != requestedWorkOrderPublicId) {
      throw const FormatException(
        'Work-order evidence response does not match the requested job',
      );
    }
    final collection = _requiredMap(json, 'feature_collection');
    if (collection['type'] != 'FeatureCollection') {
      throw const FormatException(
        'Work-order evidence response has an invalid FeatureCollection',
      );
    }
    final rawFeatures = collection['features'];
    if (rawFeatures is! List) {
      throw const FormatException(
        'Work-order evidence response has no feature cohort',
      );
    }
    final features = rawFeatures
        .map(
          (raw) => WorkOrderEvidenceFeature.fromJson(
            _castMap(raw, 'work-order evidence feature'),
            workOrderPublicId: workOrderPublicId,
          ),
        )
        .toList(growable: false);
    final declaredFeatureCount = _requiredCount(json, 'feature_count');
    if (declaredFeatureCount != features.length) {
      throw const FormatException(
        'Work-order evidence feature count does not match its cohort',
      );
    }
    final observationCount = _requiredCount(json, 'observation_count');
    final currentSourceObservationCount = _requiredCount(
      json,
      'current_source_observation_count',
    );
    final supersededSourceObservationCount = _requiredCount(
      json,
      'superseded_source_observation_count',
    );
    final projectedCurrentCount = features.fold<int>(
      0,
      (count, feature) => count + feature.currentObservationCount,
    );
    final projectedSupersededCount = features.fold<int>(
      0,
      (count, feature) => count + feature.supersededObservationCount,
    );
    final observationIds = [
      for (final feature in features) ...[
        for (final observation in feature.currentObservations)
          observation.observationId,
        for (final observation in feature.supersededObservations)
          observation.observationId,
      ],
    ];
    if (projectedCurrentCount != currentSourceObservationCount ||
        projectedSupersededCount != supersededSourceObservationCount ||
        projectedCurrentCount + projectedSupersededCount != observationCount ||
        observationIds.toSet().length != observationIds.length) {
      throw const FormatException(
        'Work-order observation totals do not match the exact feature evidence',
      );
    }
    return WorkOrderEvidenceMapSnapshot(
      reportSha256: _requiredSha(json, 'report_sha256'),
      sourceOverlaySha256: _requiredSha(json, 'source_overlay_sha256'),
      worklistReportSha256: _requiredSha(json, 'worklist_report_sha256'),
      observationEvidenceSha256: _requiredSha(
        json,
        'observation_evidence_sha256',
      ),
      workOrderId: _requiredString(json, 'work_order_id'),
      workOrderPublicId: workOrderPublicId,
      observationCount: observationCount,
      currentSourceObservationCount: currentSourceObservationCount,
      supersededSourceObservationCount: supersededSourceObservationCount,
      features: features,
      fromCache: fromCache,
      cachedAt: cachedAt?.toUtc(),
    );
  }
}

class WorkOrderEvidenceFeature {
  const WorkOrderEvidenceFeature({
    required this.id,
    required this.displayName,
    required this.assetType,
    required this.sourceSystem,
    required this.sourceProfile,
    required this.externalId,
    required this.contentSha256,
    required this.geometrySha256,
    required this.mapFeatureSha256,
    required this.workOrderEvidenceSha256,
    required this.workOrderMapFeatureSha256,
    required this.context,
    required this.contextPresentation,
    required this.geometryPresentationState,
    required this.geometryPresentation,
    required this.currentObservationCount,
    required this.supersededObservationCount,
    required this.currentObservations,
    required this.supersededObservations,
    required this.geometry,
  });

  final String id;
  final String displayName;
  final String assetType;
  final String sourceSystem;
  final String sourceProfile;
  final String? externalId;
  final String contentSha256;
  final String geometrySha256;
  final String mapFeatureSha256;
  final String workOrderEvidenceSha256;
  final String workOrderMapFeatureSha256;
  final String context;
  final StatusPresentation contextPresentation;
  final String geometryPresentationState;
  final StatusPresentation geometryPresentation;
  final int currentObservationCount;
  final int supersededObservationCount;
  final List<WorkOrderEvidenceObservation> currentObservations;
  final List<WorkOrderEvidenceObservation> supersededObservations;
  final WorkOrderEvidenceGeometry geometry;

  bool get isRenderable =>
      geometryPresentationState == 'exact_geojson' && geometry.isRenderable;
  List<LatLng> get renderedPoints => isRenderable ? geometry.points : const [];

  factory WorkOrderEvidenceFeature.fromJson(
    Map<String, dynamic> json, {
    required String workOrderPublicId,
  }) {
    if (json['type'] != 'Feature') {
      throw const FormatException('Work-order evidence contains a non-feature');
    }
    final properties = _requiredMap(json, 'properties');
    final evidence = _requiredMap(properties, 'work_order_evidence');
    if (_requiredString(evidence, 'work_order_public_id') !=
        workOrderPublicId) {
      throw const FormatException(
        'Work-order evidence feature belongs to a different job',
      );
    }
    final context = _requiredString(evidence, 'context');
    if (!workOrderEvidenceContexts.contains(context)) {
      throw const FormatException('Unknown work-order evidence context');
    }
    final contextPresentation = StatusPresentation.fromJson(
      _requiredMap(evidence, 'context_presentation'),
    );
    if (contextPresentation.value != context) {
      throw const FormatException(
        'Work-order evidence context presentation does not match its value',
      );
    }
    final geometryState = _requiredString(
      properties,
      'geometry_presentation_state',
    );
    if (!workOrderEvidenceGeometryStates.contains(geometryState)) {
      throw const FormatException(
        'Unknown work-order evidence geometry presentation',
      );
    }
    final geometryPresentation = StatusPresentation.fromJson(
      _requiredMap(properties, 'geometry_presentation'),
    );
    if (geometryPresentation.value != geometryState) {
      throw const FormatException(
        'Geometry presentation does not match its source state',
      );
    }
    final geometry = WorkOrderEvidenceGeometry.fromJson(
      _requiredMap(json, 'geometry'),
    );
    if (geometryState == 'exact_geojson' && !geometry.isRenderable) {
      throw const FormatException(
        'Exact source geometry cannot be rendered without changing it',
      );
    }
    final contentSha256 = _requiredSha(properties, 'content_sha256');
    final currentObservationCount = _requiredCount(
      evidence,
      'current_observation_count',
    );
    final supersededObservationCount = _requiredCount(
      evidence,
      'superseded_observation_count',
    );
    final currentObservations = _observationList(
      evidence,
      'current_observations',
      workOrderPublicId: workOrderPublicId,
    );
    final supersededObservations = _observationList(
      evidence,
      'superseded_observations',
      workOrderPublicId: workOrderPublicId,
    );
    if (currentObservations.length != currentObservationCount ||
        supersededObservations.length != supersededObservationCount) {
      throw const FormatException(
        'Work-order observation count does not match its exact evidence list',
      );
    }
    if (currentObservations.any(
          (observation) => observation.featureContentSha256 != contentSha256,
        ) ||
        supersededObservations.any(
          (observation) => observation.featureContentSha256 == contentSha256,
        )) {
      throw const FormatException(
        'Work-order observation content does not match its current/superseded context',
      );
    }
    return WorkOrderEvidenceFeature(
      id: _requiredString(json, 'id'),
      displayName:
          _optionalString(properties['display_name']) ??
          _optionalString(properties['external_id']) ??
          _requiredString(json, 'id'),
      assetType: _requiredString(properties, 'asset_type'),
      sourceSystem: _requiredString(properties, 'source_system'),
      sourceProfile: _requiredString(properties, 'source_profile'),
      externalId: _optionalString(properties['external_id']),
      contentSha256: contentSha256,
      geometrySha256: _requiredSha(properties, 'geometry_sha256'),
      mapFeatureSha256: _requiredSha(properties, 'map_feature_sha256'),
      workOrderEvidenceSha256: _requiredSha(
        properties,
        'work_order_evidence_sha256',
      ),
      workOrderMapFeatureSha256: _requiredSha(
        properties,
        'work_order_map_feature_sha256',
      ),
      context: context,
      contextPresentation: contextPresentation,
      geometryPresentationState: geometryState,
      geometryPresentation: geometryPresentation,
      currentObservationCount: currentObservationCount,
      supersededObservationCount: supersededObservationCount,
      currentObservations: currentObservations,
      supersededObservations: supersededObservations,
      geometry: geometry,
    );
  }
}

class WorkOrderEvidenceObservation {
  const WorkOrderEvidenceObservation({
    required this.observationId,
    required this.stagedFeatureId,
    required this.featureContentSha256,
    required this.claimSha256,
    required this.observationSha256,
    required this.verificationScope,
    required this.outcome,
    required this.observedAt,
  });

  final String observationId;
  final String stagedFeatureId;
  final String featureContentSha256;
  final String claimSha256;
  final String observationSha256;
  final String verificationScope;
  final String outcome;
  final DateTime observedAt;

  factory WorkOrderEvidenceObservation.fromJson(
    Map<String, dynamic> json, {
    required String workOrderPublicId,
  }) {
    if (_requiredString(json, 'work_order_public_id') != workOrderPublicId) {
      throw const FormatException(
        'Work-order observation belongs to a different job',
      );
    }
    final observedAtRaw = _requiredString(json, 'observed_at');
    final observedAt = DateTime.tryParse(observedAtRaw);
    if (observedAt == null) {
      throw const FormatException('observed_at must be an ISO-8601 timestamp');
    }
    return WorkOrderEvidenceObservation(
      observationId: _requiredString(json, 'observation_id'),
      stagedFeatureId: _requiredString(json, 'staged_feature_id'),
      featureContentSha256: _requiredSha(json, 'feature_content_sha256'),
      claimSha256: _requiredSha(json, 'claim_sha256'),
      observationSha256: _requiredSha(json, 'observation_sha256'),
      verificationScope: _requiredString(json, 'verification_scope'),
      outcome: _requiredString(json, 'outcome'),
      observedAt: observedAt.toUtc(),
    );
  }
}

class WorkOrderEvidenceGeometry {
  const WorkOrderEvidenceGeometry._({
    required this.type,
    this.point,
    this.line = const [],
    this.rings = const [],
  });

  final String type;
  final LatLng? point;
  final List<LatLng> line;
  final List<List<LatLng>> rings;

  bool get isRenderable => switch (type) {
    'Point' => point != null,
    'LineString' => line.length >= 2,
    'Polygon' => rings.isNotEmpty && rings.every((ring) => ring.length >= 4),
    _ => false,
  };
  List<LatLng> get points => switch (type) {
    'Point' => point == null ? const [] : [point!],
    'LineString' => line,
    'Polygon' => [for (final ring in rings) ...ring],
    _ => const [],
  };

  factory WorkOrderEvidenceGeometry.fromJson(Map<String, dynamic> json) {
    final type = _requiredString(json, 'type');
    final coordinates = json['coordinates'];
    return switch (type) {
      'Point' => WorkOrderEvidenceGeometry._(
        type: type,
        point: _position(coordinates),
      ),
      'LineString' => WorkOrderEvidenceGeometry._(
        type: type,
        line: _positions(coordinates),
      ),
      'Polygon' => WorkOrderEvidenceGeometry._(
        type: type,
        rings: _rings(coordinates),
      ),
      _ => WorkOrderEvidenceGeometry._(type: type),
    };
  }
}

Map<String, dynamic> _requiredMap(Map<String, dynamic> json, String field) =>
    _castMap(json[field], field);

Map<String, dynamic> _castMap(Object? value, String field) {
  if (value is! Map) throw FormatException('$field must be an object');
  return value.cast<String, dynamic>();
}

String _requiredString(Map<String, dynamic> json, String field) {
  final value = _optionalString(json[field]);
  if (value == null) throw FormatException('$field must be a non-empty string');
  return value;
}

String? _optionalString(Object? value) {
  if (value is! String || value.trim().isEmpty) return null;
  return value.trim();
}

int _requiredCount(Map<String, dynamic> json, String field) {
  final value = json[field];
  if (value is! int || value < 0) {
    throw FormatException('$field must be a non-negative integer');
  }
  return value;
}

final _shaPattern = RegExp(r'^[0-9a-f]{64}$');

String _requiredSha(Map<String, dynamic> json, String field) {
  final value = _requiredString(json, field);
  if (!_shaPattern.hasMatch(value)) {
    throw FormatException('$field must be a lowercase SHA-256 digest');
  }
  return value;
}

LatLng? _position(Object? value) {
  if (value is! List || value.length < 2) return null;
  final longitude = value[0];
  final latitude = value[1];
  if (latitude is! num || longitude is! num) return null;
  return safeLatLng(latitude.toDouble(), longitude.toDouble());
}

List<LatLng> _positions(Object? value) {
  if (value is! List) return const [];
  final result = value.map(_position).toList();
  if (result.any((point) => point == null)) return const [];
  return result.whereType<LatLng>().toList(growable: false);
}

List<List<LatLng>> _rings(Object? value) {
  if (value is! List || value.isEmpty) return const [];
  final result = value.map(_positions).toList(growable: false);
  if (result.any((ring) => ring.length < 4 || ring.first != ring.last)) {
    return const [];
  }
  return result;
}

List<WorkOrderEvidenceObservation> _observationList(
  Map<String, dynamic> evidence,
  String field, {
  required String workOrderPublicId,
}) {
  final value = evidence[field];
  if (value is! List) {
    throw FormatException('$field must be an exact observation list');
  }
  return value
      .map(
        (row) => WorkOrderEvidenceObservation.fromJson(
          _castMap(row, field),
          workOrderPublicId: workOrderPublicId,
        ),
      )
      .toList(growable: false);
}
