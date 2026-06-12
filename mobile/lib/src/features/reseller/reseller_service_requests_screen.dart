import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:geolocator/geolocator.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:latlong2/latlong.dart';

import '../../core/formatters.dart';
import '../../models/reseller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// The reseller's new-service / installation requests: list with status +
/// serviceability, and a submission form (lead or existing customer) with a
/// map pin for the install location.
class ResellerServiceRequestsScreen extends ConsumerWidget {
  const ResellerServiceRequestsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final requests = ref.watch(resellerServiceRequestsProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Service requests')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () async {
          final created = await showModalBottomSheet<ResellerServiceRequest>(
            context: context,
            isScrollControlled: true,
            builder: (_) => const _NewRequestSheet(),
          );
          if (created != null) {
            ref.invalidate(resellerServiceRequestsProvider);
            if (context.mounted) {
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(
                  content: Text(switch (created.serviceability) {
                    'serviceable' =>
                      'Request submitted — location is near our network'
                          '${created.nearestPlantKm != null ? ' (${created.nearestPlantKm} km)' : ''}.',
                    'not_serviceable' =>
                      'Request submitted — note: location looks far from our '
                          'network; our team will confirm.',
                    _ => 'Request submitted.',
                  }),
                ),
              );
            }
          }
        },
        icon: const Icon(Icons.add),
        label: const Text('New request'),
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(resellerServiceRequestsProvider);
          await ref.read(resellerServiceRequestsProvider.future);
        },
        child: AsyncValueView<List<ResellerServiceRequest>>(
          value: requests,
          onRetry: () => ref.invalidate(resellerServiceRequestsProvider),
          data: (items) => items.isEmpty
              ? ListView(
                  children: const [
                    Padding(
                      padding: EdgeInsets.symmetric(vertical: 48),
                      child: EmptyState(
                        icon: Icons.assignment_outlined,
                        message:
                            'No service requests yet — submit one '
                            'with the button below.',
                      ),
                    ),
                  ],
                )
              : ListView(
                  padding: const EdgeInsets.all(12),
                  children: [for (final r in items) _RequestTile(request: r)],
                ),
        ),
      ),
    );
  }
}

class _RequestTile extends StatelessWidget {
  const _RequestTile({required this.request});

  final ResellerServiceRequest request;

  @override
  Widget build(BuildContext context) {
    final r = request;
    final theme = Theme.of(context);
    final statusColor = switch (r.status) {
      'completed' => Colors.green.shade700,
      'rejected' => theme.colorScheme.error,
      'scheduled' => theme.colorScheme.primary,
      _ => theme.colorScheme.outline,
    };
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        title: Text(r.contactName ?? r.address ?? 'Service request'),
        subtitle: Text(
          [
            if (r.address != null) r.address!,
            if (r.createdAt != null) Fmt.date(r.createdAt!),
            if (r.adminNotes != null) '“${r.adminNotes}”',
          ].join('\n'),
        ),
        isThreeLine: r.adminNotes != null,
        trailing: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Text(
              r.status.replaceAll('_', ' '),
              style: theme.textTheme.labelMedium?.copyWith(
                color: statusColor,
                fontWeight: FontWeight.w700,
              ),
            ),
            if (r.serviceability != 'unknown')
              Text(
                r.serviceability == 'serviceable' ? 'in coverage' : 'far',
                style: theme.textTheme.labelSmall?.copyWith(
                  color: theme.colorScheme.outline,
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _NewRequestSheet extends ConsumerStatefulWidget {
  const _NewRequestSheet();

  @override
  ConsumerState<_NewRequestSheet> createState() => _NewRequestSheetState();
}

class _NewRequestSheetState extends ConsumerState<_NewRequestSheet> {
  final _name = TextEditingController();
  final _phone = TextEditingController();
  final _email = TextEditingController();
  final _address = TextEditingController();
  final _notes = TextEditingController();
  LatLng? _pin;
  final _mapController = MapController();
  bool _locating = false;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    for (final c in [_name, _phone, _email, _address, _notes]) {
      c.dispose();
    }
    super.dispose();
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
      final point = LatLng(position.latitude, position.longitude);
      setState(() => _pin = point);
      _mapController.move(point, 17);
    } on LocationServiceDisabledException {
      setState(
        () => _error = 'Turn on location services to use your GPS position.',
      );
    } catch (_) {
      setState(
        () =>
            _error = 'Location permission is needed to use your GPS position.',
      );
    } finally {
      if (mounted) setState(() => _locating = false);
    }
  }

  Future<void> _submit() async {
    if (_name.text.trim().isEmpty || _phone.text.trim().isEmpty) {
      setState(() => _error = 'Contact name and phone are required.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final created = await ref
          .read(resellerRepositoryProvider)
          .createServiceRequest(
            contactName: _name.text.trim(),
            contactPhone: _phone.text.trim(),
            contactEmail: _email.text.trim(),
            address: _address.text.trim(),
            latitude: _pin?.latitude,
            longitude: _pin?.longitude,
            notes: _notes.text.trim(),
          );
      if (mounted) Navigator.of(context).pop(created);
    } catch (_) {
      setState(() {
        _busy = false;
        _error = 'Could not submit the request — please try again.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        left: 16,
        right: 16,
        top: 16,
        bottom: 16 + MediaQuery.of(context).viewInsets.bottom,
      ),
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Request new service',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _name,
              decoration: const InputDecoration(
                labelText: 'Contact name *',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _phone,
              keyboardType: TextInputType.phone,
              decoration: const InputDecoration(
                labelText: 'Contact phone *',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _email,
              keyboardType: TextInputType.emailAddress,
              decoration: const InputDecoration(
                labelText: 'Contact email',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _address,
              decoration: const InputDecoration(
                labelText: 'Install address',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'Tap the map to pin the install location (optional)',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 4),
            SizedBox(
              height: 180,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Stack(
                  children: [
                    FlutterMap(
                      mapController: _mapController,
                      options: MapOptions(
                        initialCenter: _pin ?? const LatLng(6.5244, 3.3792),
                        initialZoom: 11,
                        onTap: (_, point) => setState(() => _pin = point),
                      ),
                      children: [
                        TileLayer(
                          urlTemplate:
                              'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                          userAgentPackageName: 'io.dotmac.selfcare',
                        ),
                        if (_pin != null)
                          MarkerLayer(
                            markers: [
                              Marker(
                                point: _pin!,
                                width: 36,
                                height: 36,
                                child: const Icon(
                                  Icons.location_on,
                                  color: Colors.red,
                                  size: 32,
                                ),
                              ),
                            ],
                          ),
                      ],
                    ),
                    Positioned(
                      right: 8,
                      bottom: 8,
                      child: FloatingActionButton.small(
                        heroTag: 'sr-gps',
                        onPressed: _locating ? null : _useMyLocation,
                        tooltip: 'Use my current location',
                        child: _locating
                            ? const SizedBox(
                                height: 16,
                                width: 16,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.my_location, size: 18),
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _notes,
              maxLines: 2,
              decoration: const InputDecoration(
                labelText: 'Notes',
                border: OutlineInputBorder(),
              ),
            ),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(
                _error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ],
            const SizedBox(height: 12),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: _busy ? null : () => Navigator.of(context).pop(),
                  child: const Text('Cancel'),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: _busy ? null : _submit,
                  child: Text(_busy ? 'Submitting…' : 'Submit'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
