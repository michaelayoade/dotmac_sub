import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:latlong2/latlong.dart';

import '../../app/theme.dart';
import '../../core/location/map_coordinates.dart';
import 'job_models.dart';
import 'jobs_providers.dart';

class LocationPinScreen extends ConsumerStatefulWidget {
  const LocationPinScreen({
    super.key,
    required this.jobId,
    required this.initialLocation,
    this.showTiles = true,
  });

  final String jobId;
  final JobLocation initialLocation;
  final bool showTiles;

  @override
  ConsumerState<LocationPinScreen> createState() => _LocationPinScreenState();
}

class _LocationPinScreenState extends ConsumerState<LocationPinScreen> {
  late LatLng _selected;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _selected =
        safeLatLng(
          widget.initialLocation.latitude,
          widget.initialLocation.longitude,
        ) ??
        defaultMapCenter;
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    try {
      await ref
          .read(jobsRepositoryProvider)
          .updateLocation(
            jobId: widget.jobId,
            latitude: _selected.latitude,
            longitude: _selected.longitude,
          );
      ref
        ..invalidate(jobDetailProvider(widget.jobId))
        ..invalidate(jobsListProvider);
      if (mounted) Navigator.of(context).pop(true);
    } catch (_) {
      if (mounted) {
        setState(() => _saving = false);
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not save location')),
        );
      }
    }
  }

  void _selectPoint(LatLng point) {
    final safePoint = safeLatLng(point.latitude, point.longitude);
    if (safePoint == null) return;
    setState(() => _selected = safePoint);
  }

  @override
  Widget build(BuildContext context) {
    final textTheme = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Edit job pin')),
      body: Stack(
        children: [
          FlutterMap(
            options: MapOptions(
              initialCenter: _selected,
              initialZoom: widget.initialLocation.hasCoordinates ? 15 : 12,
              cameraConstraint: finiteMapCameraConstraint,
              onTap: (_, point) => _selectPoint(point),
            ),
            children: [
              if (widget.showTiles)
                TileLayer(
                  urlTemplate: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                  userAgentPackageName: 'io.dotmac.dotmac_field',
                ),
              MarkerLayer(
                markers: [
                  Marker(
                    point: _selected,
                    width: 48,
                    height: 48,
                    child: const Icon(
                      Icons.location_pin,
                      size: 44,
                      color: AppColors.accent,
                    ),
                  ),
                ],
              ),
              if (widget.showTiles)
                const Align(
                  alignment: Alignment.bottomLeft,
                  child: Padding(
                    padding: EdgeInsets.all(4),
                    child: Text(
                      '© OpenStreetMap contributors',
                      style: TextStyle(fontSize: 10),
                    ),
                  ),
                ),
            ],
          ),
        ],
      ),
      bottomNavigationBar: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                '${_selected.latitude.toStringAsFixed(6)}, ${_selected.longitude.toStringAsFixed(6)}',
                textAlign: TextAlign.center,
                style: textTheme.bodySmall,
              ),
              const SizedBox(height: 10),
              FilledButton.icon(
                onPressed: _saving ? null : _save,
                icon: _saving
                    ? const SizedBox.square(
                        dimension: 20,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.push_pin_outlined),
                label: Text(_saving ? 'Saving...' : 'Save pin location'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
