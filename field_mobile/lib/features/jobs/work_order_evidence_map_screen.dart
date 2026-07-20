import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/status_presentation.dart';
import '../../app/theme.dart';
import '../../core/location/map_coordinates.dart';
import 'work_order_evidence_map_models.dart';
import 'work_order_evidence_map_repository.dart';

class WorkOrderEvidenceMapScreen extends ConsumerWidget {
  const WorkOrderEvidenceMapScreen({
    super.key,
    required this.workOrderPublicId,
    this.showTiles = true,
  });

  final String workOrderPublicId;

  /// Disabled in widget tests so no tile HTTP requests are made.
  final bool showTiles;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final snapshot = ref.watch(workOrderEvidenceMapProvider(workOrderPublicId));
    return Scaffold(
      appBar: AppBar(
        title: const Text('Fiber evidence'),
        actions: [
          IconButton(
            key: const Key('refresh-work-order-evidence-map'),
            tooltip: 'Refresh exact evidence',
            onPressed: () =>
                ref.invalidate(workOrderEvidenceMapProvider(workOrderPublicId)),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: snapshot.when(
        data: (value) =>
            _EvidenceMapBody(snapshot: value, showTiles: showTiles),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) => _EvidenceMapError(
          onRetry: () =>
              ref.invalidate(workOrderEvidenceMapProvider(workOrderPublicId)),
        ),
      ),
    );
  }
}

class _EvidenceMapBody extends StatelessWidget {
  const _EvidenceMapBody({required this.snapshot, required this.showTiles});

  final WorkOrderEvidenceMapSnapshot snapshot;
  final bool showTiles;

  @override
  Widget build(BuildContext context) {
    return ListView(
      key: const Key('work-order-evidence-list'),
      padding: const EdgeInsets.all(AppSpace.lg),
      children: [
        if (snapshot.fromCache) ...[
          _OfflineEvidenceBanner(snapshot: snapshot),
          const SizedBox(height: AppSpace.md),
        ],
        _EvidenceSummary(snapshot: snapshot),
        const SizedBox(height: AppSpace.md),
        if (snapshot.features.isEmpty)
          const _EmptyEvidenceCard()
        else ...[
          _ExactEvidenceMap(snapshot: snapshot, showTiles: showTiles),
          const SizedBox(height: AppSpace.md),
          Text(
            'Observed fiber assets',
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: AppSpace.sm),
          for (final feature in snapshot.features) ...[
            _EvidenceFeatureCard(feature: feature),
            const SizedBox(height: AppSpace.sm),
          ],
        ],
      ],
    );
  }
}

class _OfflineEvidenceBanner extends StatelessWidget {
  const _OfflineEvidenceBanner({required this.snapshot});

  final WorkOrderEvidenceMapSnapshot snapshot;

  @override
  Widget build(BuildContext context) {
    final color = AppColors.statusTone(context, StatusTone.warning);
    return Semantics(
      label: 'Offline cached fiber evidence may be stale until refreshed',
      child: Container(
        key: const Key('cached-evidence-warning'),
        padding: const EdgeInsets.all(AppSpace.md),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.10),
          border: Border.all(color: color.withValues(alpha: 0.45)),
          borderRadius: BorderRadius.circular(AppRadii.control),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.offline_bolt_outlined, color: color),
            const SizedBox(width: AppSpace.sm),
            Expanded(
              child: Text(
                'Offline snapshot${_cachedAtSuffix(snapshot.cachedAt)}. '
                'This exact job report may be stale until refreshed.',
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _EvidenceSummary extends StatelessWidget {
  const _EvidenceSummary({required this.snapshot});

  final WorkOrderEvidenceMapSnapshot snapshot;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(AppSpace.lg),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Exact job-scoped evidence',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: AppSpace.sm),
            Wrap(
              spacing: AppSpace.sm,
              runSpacing: AppSpace.sm,
              children: [
                _CountChip(
                  key: const Key('current-source-count'),
                  label: 'Current source',
                  value: snapshot.currentSourceObservationCount,
                ),
                _CountChip(
                  key: const Key('superseded-source-count'),
                  label: 'Superseded source',
                  value: snapshot.supersededSourceObservationCount,
                ),
                _CountChip(
                  key: const Key('unrenderable-feature-count'),
                  label: 'Unrenderable unchanged',
                  value: snapshot.unrenderableFeatureCount,
                ),
              ],
            ),
            const SizedBox(height: AppSpace.md),
            _HashRow(label: 'Report SHA-256', value: snapshot.reportSha256),
            const SizedBox(height: AppSpace.sm),
            _HashRow(
              label: 'Source overlay SHA-256',
              value: snapshot.sourceOverlaySha256,
            ),
            const SizedBox(height: AppSpace.sm),
            _HashRow(
              label: 'Worklist report SHA-256',
              value: snapshot.worklistReportSha256,
            ),
            const SizedBox(height: AppSpace.sm),
            _HashRow(
              label: 'Observation evidence SHA-256',
              value: snapshot.observationEvidenceSha256,
            ),
          ],
        ),
      ),
    );
  }
}

class _CountChip extends StatelessWidget {
  const _CountChip({super.key, required this.label, required this.value});

  final String label;
  final int value;

  @override
  Widget build(BuildContext context) {
    return Chip(label: Text('$label: $value'));
  }
}

class _HashRow extends StatelessWidget {
  const _HashRow({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: Theme.of(context).textTheme.labelMedium),
        const SizedBox(height: AppSpace.xs),
        SelectableText(
          value,
          style: Theme.of(
            context,
          ).textTheme.bodySmall?.copyWith(fontFamily: 'monospace'),
        ),
      ],
    );
  }
}

class _ExactEvidenceMap extends StatelessWidget {
  const _ExactEvidenceMap({required this.snapshot, required this.showTiles});

  final WorkOrderEvidenceMapSnapshot snapshot;
  final bool showTiles;

  @override
  Widget build(BuildContext context) {
    final points = snapshot.allRenderedPoints;
    if (points.isEmpty) {
      return Card(
        key: const Key('no-renderable-evidence-map'),
        child: const Padding(
          padding: EdgeInsets.all(AppSpace.lg),
          child: Text(
            'The source geometry cannot be rendered unchanged. Its evidence '
            'and hashes remain listed below.',
          ),
        ),
      );
    }
    return Card(
      clipBehavior: Clip.antiAlias,
      child: SizedBox(
        key: const Key('exact-work-order-evidence-map'),
        height: 340,
        child: FlutterMap(
          options: MapOptions(
            initialCenter: points.first,
            initialZoom: 16,
            initialCameraFit: points.length > 1
                ? CameraFit.coordinates(
                    coordinates: points,
                    padding: const EdgeInsets.all(32),
                    maxZoom: 18,
                  )
                : null,
            cameraConstraint: finiteMapCameraConstraint,
          ),
          children: [
            if (showTiles)
              TileLayer(
                urlTemplate: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                userAgentPackageName: 'io.dotmac.dotmac_field',
              ),
            PolygonLayer(
              polygons: [
                for (final feature in snapshot.features)
                  if (feature.isRenderable &&
                      feature.geometry.type == 'Polygon')
                    Polygon(
                      points: feature.geometry.rings.first,
                      holePointsList: feature.geometry.rings.length > 1
                          ? feature.geometry.rings.sublist(1)
                          : null,
                      color: _featureColor(
                        context,
                        feature,
                      ).withValues(alpha: 0.18),
                      borderColor: _featureColor(context, feature),
                      borderStrokeWidth: 3,
                    ),
              ],
            ),
            PolylineLayer(
              polylines: [
                for (final feature in snapshot.features)
                  if (feature.isRenderable &&
                      feature.geometry.type == 'LineString')
                    Polyline(
                      points: feature.geometry.line,
                      color: _featureColor(context, feature),
                      strokeWidth: 5,
                    ),
              ],
            ),
            MarkerLayer(
              markers: [
                for (final feature in snapshot.features)
                  if (feature.isRenderable && feature.geometry.type == 'Point')
                    Marker(
                      point: feature.geometry.point!,
                      width: 42,
                      height: 42,
                      child: Semantics(
                        label: feature.displayName,
                        child: Icon(
                          Icons.location_pin,
                          size: 38,
                          color: _featureColor(context, feature),
                        ),
                      ),
                    ),
              ],
            ),
            if (showTiles)
              const Align(
                alignment: Alignment.bottomLeft,
                child: Padding(
                  padding: EdgeInsets.all(AppSpace.xs),
                  child: Text(
                    '© OpenStreetMap contributors',
                    style: TextStyle(fontSize: 10),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _EvidenceFeatureCard extends StatelessWidget {
  const _EvidenceFeatureCard({required this.feature});

  final WorkOrderEvidenceFeature feature;

  @override
  Widget build(BuildContext context) {
    return Card(
      key: Key('work-order-evidence-feature-${feature.id}'),
      child: Padding(
        padding: const EdgeInsets.all(AppSpace.lg),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              feature.displayName,
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: AppSpace.xs),
            Text(
              '${feature.assetType} · ${feature.sourceSystem} / '
              '${feature.sourceProfile}',
            ),
            const SizedBox(height: AppSpace.sm),
            Wrap(
              spacing: AppSpace.sm,
              runSpacing: AppSpace.sm,
              children: [
                _PresentationChip(
                  key: Key('evidence-context-${feature.id}'),
                  presentation: feature.contextPresentation,
                ),
                _PresentationChip(
                  key: Key('geometry-state-${feature.id}'),
                  presentation: feature.geometryPresentation,
                ),
              ],
            ),
            const SizedBox(height: AppSpace.sm),
            Text(
              '${feature.currentObservationCount} current · '
              '${feature.supersededObservationCount} superseded observations',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: AppSpace.sm),
            ExpansionTile(
              key: Key('evidence-hashes-${feature.id}'),
              tilePadding: EdgeInsets.zero,
              childrenPadding: EdgeInsets.zero,
              title: const Text('Evidence hashes'),
              children: [
                _HashRow(
                  label: 'Content SHA-256',
                  value: feature.contentSha256,
                ),
                const SizedBox(height: AppSpace.sm),
                _HashRow(
                  label: 'Geometry SHA-256',
                  value: feature.geometrySha256,
                ),
                const SizedBox(height: AppSpace.sm),
                _HashRow(
                  label: 'Source map feature SHA-256',
                  value: feature.mapFeatureSha256,
                ),
                const SizedBox(height: AppSpace.sm),
                _HashRow(
                  label: 'Work-order evidence SHA-256',
                  value: feature.workOrderEvidenceSha256,
                ),
                const SizedBox(height: AppSpace.sm),
                _HashRow(
                  label: 'Work-order map feature SHA-256',
                  value: feature.workOrderMapFeatureSha256,
                ),
              ],
            ),
            ExpansionTile(
              key: Key('immutable-observations-${feature.id}'),
              tilePadding: EdgeInsets.zero,
              childrenPadding: EdgeInsets.zero,
              title: const Text('Immutable observations'),
              children: [
                for (final observation in feature.currentObservations)
                  _ObservationEvidence(
                    contextLabel: 'Current source observation',
                    observation: observation,
                  ),
                for (final observation in feature.supersededObservations)
                  _ObservationEvidence(
                    contextLabel: 'Superseded source observation',
                    observation: observation,
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ObservationEvidence extends StatelessWidget {
  const _ObservationEvidence({
    required this.contextLabel,
    required this.observation,
  });

  final String contextLabel;
  final WorkOrderEvidenceObservation observation;

  @override
  Widget build(BuildContext context) {
    return Padding(
      key: Key('immutable-observation-${observation.observationId}'),
      padding: const EdgeInsets.only(bottom: AppSpace.md),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(contextLabel, style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: AppSpace.xs),
          SelectableText(observation.observationId),
          const SizedBox(height: AppSpace.xs),
          Text(
            '${observation.verificationScope} · ${observation.outcome} · '
            '${observation.observedAt.toLocal()}',
          ),
          const SizedBox(height: AppSpace.sm),
          _HashRow(
            label: 'Observed content SHA-256',
            value: observation.featureContentSha256,
          ),
          const SizedBox(height: AppSpace.sm),
          _HashRow(label: 'Claim SHA-256', value: observation.claimSha256),
          const SizedBox(height: AppSpace.sm),
          _HashRow(
            label: 'Observation SHA-256',
            value: observation.observationSha256,
          ),
        ],
      ),
    );
  }
}

class _PresentationChip extends StatelessWidget {
  const _PresentationChip({super.key, required this.presentation});

  final StatusPresentation presentation;

  @override
  Widget build(BuildContext context) {
    final color = AppColors.statusTone(context, presentation.tone);
    return Chip(
      avatar: Icon(_statusIcon(presentation.icon), size: 18, color: color),
      label: Text(presentation.label),
      side: BorderSide(color: color.withValues(alpha: 0.45)),
      backgroundColor: color.withValues(alpha: 0.08),
    );
  }
}

class _EmptyEvidenceCard extends StatelessWidget {
  const _EmptyEvidenceCard();

  @override
  Widget build(BuildContext context) {
    return const Card(
      key: Key('empty-work-order-evidence-map'),
      child: Padding(
        padding: EdgeInsets.all(AppSpace.xl),
        child: Column(
          children: [
            Icon(Icons.layers_clear_outlined, size: 40),
            SizedBox(height: AppSpace.sm),
            Text(
              'No immutable fiber observations are attached to this job.',
              textAlign: TextAlign.center,
            ),
            SizedBox(height: AppSpace.xs),
            Text(
              'No unobserved assets or likely fault areas are inferred here.',
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }
}

class _EvidenceMapError extends StatelessWidget {
  const _EvidenceMapError({required this.onRetry});

  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(AppSpace.xl),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.warning_amber_outlined, size: 40),
            const SizedBox(height: AppSpace.sm),
            const Text(
              'Could not load an exact evidence map for this job.',
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: AppSpace.md),
            OutlinedButton.icon(
              key: const Key('retry-work-order-evidence-map'),
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Retry'),
            ),
          ],
        ),
      ),
    );
  }
}

Color _featureColor(BuildContext context, WorkOrderEvidenceFeature feature) =>
    AppColors.statusTone(context, feature.contextPresentation.tone);

IconData _statusIcon(String icon) => switch (icon) {
  'check' => Icons.check_circle_outline,
  'clock' => Icons.schedule_outlined,
  'alert' => Icons.warning_amber_outlined,
  'x' => Icons.cancel_outlined,
  'minus' => Icons.remove_circle_outline,
  'archive' => Icons.archive_outlined,
  _ => Icons.info_outline,
};

String _cachedAtSuffix(DateTime? cachedAt) {
  if (cachedAt == null) return '';
  return ' from ${cachedAt.toLocal()}';
}
