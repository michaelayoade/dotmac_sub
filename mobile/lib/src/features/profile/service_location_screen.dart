import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';
import 'package:latlong2/latlong.dart';

import '../../core/api_exception.dart';
import '../../models/service_location.dart';
import '../../providers/data_providers.dart';

/// Validate the service-address map pin: see the approved pin, move it (tap
/// the map or use the device GPS), and submit a correction for admin review.
/// Mirrors the web portal's /portal/location page.
class ServiceLocationScreen extends ConsumerStatefulWidget {
  const ServiceLocationScreen({super.key});

  @override
  ConsumerState<ServiceLocationScreen> createState() =>
      _ServiceLocationScreenState();
}

class _ServiceLocationScreenState extends ConsumerState<ServiceLocationScreen> {
  // Lagos as a sane fallback when no pin is on file yet.
  static const _fallbackCenter = LatLng(6.5244, 3.3792);

  final _mapController = MapController();
  final _note = TextEditingController();
  LatLng? _selected;
  String? _resolvedAddress;
  Timer? _reverseDebounce;
  bool _locating = false;
  bool _submitting = false;

  @override
  void dispose() {
    _reverseDebounce?.cancel();
    _note.dispose();
    super.dispose();
  }

  void _select(LatLng point, {bool recenter = false}) {
    setState(() {
      _selected = point;
      _resolvedAddress = null;
    });
    if (recenter) _mapController.move(point, 17);
    _reverseDebounce?.cancel();
    _reverseDebounce = Timer(const Duration(milliseconds: 400), () async {
      try {
        final address = await ref
            .read(locationRepositoryProvider)
            .reverseGeocode(point.latitude, point.longitude);
        if (mounted) setState(() => _resolvedAddress = address);
      } on ApiException {
        // Best-effort label; the pin itself is what gets submitted.
      }
    });
  }

  Future<void> _useMyLocation() async {
    setState(() => _locating = true);
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        throw const LocationServiceDisabledException();
      }
      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }
      if (permission == LocationPermission.denied ||
          permission == LocationPermission.deniedForever) {
        throw const PermissionDeniedException('denied');
      }
      final position = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 15),
        ),
      );
      if (!mounted) return;
      _select(LatLng(position.latitude, position.longitude), recenter: true);
    } on LocationServiceDisabledException {
      _snack('Turn on location services to use your GPS position.');
    } catch (_) {
      _snack('Location permission is needed to use your GPS position.');
    } finally {
      if (mounted) setState(() => _locating = false);
    }
  }

  void _snack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _submit() async {
    final selected = _selected;
    if (selected == null) return;
    setState(() => _submitting = true);
    try {
      await ref.read(locationRepositoryProvider).submitCorrection(
            latitude: selected.latitude,
            longitude: selected.longitude,
            note: _note.text.trim(),
          );
      _note.clear();
      setState(() => _selected = null);
      ref.invalidate(serviceLocationProvider);
      _snack('Correction submitted for review.');
    } on ApiException catch (e) {
      _snack(e.message);
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _cancel(String requestId) async {
    try {
      await ref.read(locationRepositoryProvider).cancelRequest(requestId);
      ref.invalidate(serviceLocationProvider);
      _snack('Pending correction canceled.');
    } on ApiException catch (e) {
      _snack(e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final location = ref.watch(serviceLocationProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Service location')),
      body: location.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => Center(
          child: Text(e is ApiException ? e.message : 'Failed to load'),
        ),
        data: (data) => _body(context, data),
      ),
    );
  }

  Widget _body(BuildContext context, ServiceLocation data) {
    final theme = Theme.of(context);
    final pending = data.pendingRequest;
    final current =
        data.hasPin ? LatLng(data.latitude!, data.longitude!) : null;
    final pendingPoint =
        pending != null ? LatLng(pending.latitude, pending.longitude) : null;
    final canEdit = data.canSubmitRequest;
    final center = _selected ?? pendingPoint ?? current ?? _fallbackCenter;

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (data.addressLabel != null) ...[
          Text('Address on file', style: theme.textTheme.labelLarge),
          const SizedBox(height: 4),
          Text(data.addressLabel!, style: theme.textTheme.bodyMedium),
          const SizedBox(height: 12),
        ],
        ClipRRect(
          borderRadius: BorderRadius.circular(16),
          child: SizedBox(
            height: 320,
            child: Stack(
              children: [
                FlutterMap(
                  mapController: _mapController,
                  options: MapOptions(
                    initialCenter: center,
                    initialZoom: data.hasPin || _selected != null ? 16 : 11,
                    onTap: canEdit ? (_, point) => _select(point) : null,
                  ),
                  children: [
                    TileLayer(
                      urlTemplate:
                          'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                      userAgentPackageName: 'io.dotmac.selfcare',
                    ),
                    MarkerLayer(
                      markers: [
                        if (current != null)
                          Marker(
                            point: current,
                            width: 36,
                            height: 36,
                            child: Icon(
                              Icons.home,
                              color: theme.colorScheme.primary,
                              size: 32,
                            ),
                          ),
                        if (pendingPoint != null)
                          Marker(
                            point: pendingPoint,
                            width: 36,
                            height: 36,
                            child: const Icon(
                              Icons.hourglass_top,
                              color: Colors.amber,
                              size: 30,
                            ),
                          ),
                        if (_selected != null)
                          Marker(
                            point: _selected!,
                            width: 40,
                            height: 40,
                            alignment: Alignment.topCenter,
                            child: const Icon(
                              Icons.place,
                              color: Colors.red,
                              size: 40,
                            ),
                          ),
                      ],
                    ),
                  ],
                ),
                if (canEdit)
                  Positioned(
                    right: 12,
                    bottom: 12,
                    child: FloatingActionButton.small(
                      heroTag: 'gps',
                      onPressed: _locating ? null : _useMyLocation,
                      tooltip: 'Use my current location',
                      child: _locating
                          ? const SizedBox(
                              height: 18,
                              width: 18,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Icon(Icons.my_location),
                    ),
                  ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(
          canEdit
              ? 'Tap the map (or use the GPS button) to place the pin where '
                  'your service is actually installed.'
              : pending != null
                  ? 'A correction is waiting for review. Cancel it to '
                      'submit a different one.'
                  : 'No service address is on file yet — contact support '
                      'first so the address record can be created.',
          style: theme.textTheme.bodySmall,
        ),
        if (_selected != null) ...[
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Selected position', style: theme.textTheme.labelLarge),
                  const SizedBox(height: 4),
                  Text(
                    '${_selected!.latitude.toStringAsFixed(6)}, '
                    '${_selected!.longitude.toStringAsFixed(6)}',
                    style: theme.textTheme.bodyMedium,
                  ),
                  if (_resolvedAddress != null) ...[
                    const SizedBox(height: 4),
                    Text(
                      '≈ $_resolvedAddress',
                      style: theme.textTheme.bodySmall,
                    ),
                  ],
                  const SizedBox(height: 12),
                  TextField(
                    controller: _note,
                    maxLines: 3,
                    minLines: 1,
                    decoration: const InputDecoration(
                      labelText: "What's wrong with the current pin?",
                      hintText: 'Example: the pin is on the next street over.',
                    ),
                  ),
                  const SizedBox(height: 12),
                  FilledButton(
                    onPressed: _submitting ? null : _submit,
                    child: _submitting
                        ? const SizedBox(
                            height: 20,
                            width: 20,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Text('Submit for review'),
                  ),
                ],
              ),
            ),
          ),
        ],
        if (pending != null) ...[
          const SizedBox(height: 12),
          Card(
            child: ListTile(
              leading: const Icon(Icons.hourglass_top, color: Colors.amber),
              title: const Text('Pending review'),
              subtitle: Text(
                '${pending.latitude.toStringAsFixed(6)}, '
                '${pending.longitude.toStringAsFixed(6)}'
                '${pending.note != null ? '\n${pending.note}' : ''}',
              ),
              isThreeLine: pending.note != null,
              trailing: TextButton(
                onPressed: () => _cancel(pending.id),
                child: const Text('Cancel'),
              ),
            ),
          ),
        ],
        if (data.history.isNotEmpty) ...[
          const SizedBox(height: 16),
          Text('History', style: theme.textTheme.labelLarge),
          const SizedBox(height: 4),
          for (final item in data.history)
            ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text(
                '${item.status[0].toUpperCase()}${item.status.substring(1)}'
                ' — ${item.latitude.toStringAsFixed(5)}, '
                '${item.longitude.toStringAsFixed(5)}',
              ),
              subtitle: item.reviewNote != null
                  ? Text('Review note: ${item.reviewNote}')
                  : null,
            ),
        ],
      ],
    );
  }
}
