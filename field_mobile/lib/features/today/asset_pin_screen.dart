import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:latlong2/latlong.dart';

import '../../app/theme.dart';
import '../../core/location/map_coordinates.dart';
import 'map_assets_repository.dart';
import 'map_models.dart';

class AssetPinScreen extends ConsumerStatefulWidget {
  const AssetPinScreen({super.key, required this.asset, this.showTiles = true});

  final MapAsset asset;
  final bool showTiles;

  @override
  ConsumerState<AssetPinScreen> createState() => _AssetPinScreenState();
}

class _AssetPinScreenState extends ConsumerState<AssetPinScreen> {
  late LatLng _selected;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _selected =
        safeLatLng(widget.asset.latitude, widget.asset.longitude) ??
        defaultMapCenter;
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    try {
      await ref
          .read(mapAssetsRepositoryProvider)
          .updateLocation(
            type: widget.asset.type,
            id: widget.asset.id,
            latitude: _selected.latitude,
            longitude: _selected.longitude,
          );
      ref.invalidate(mapAssetsProvider);
      if (mounted) Navigator.of(context).pop(true);
    } catch (_) {
      if (mounted) {
        setState(() => _saving = false);
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Could not save asset location')),
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
    final label = mapAssetTypeLabels[widget.asset.type] ?? widget.asset.type;
    return Scaffold(
      appBar: AppBar(title: Text('Edit $label pin')),
      body: FlutterMap(
        options: MapOptions(
          initialCenter: _selected,
          initialZoom: 15,
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
                child: Icon(
                  Icons.location_pin,
                  size: 44,
                  color: AppColors.accent,
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
