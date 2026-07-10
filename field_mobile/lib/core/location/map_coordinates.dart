import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';

const defaultMapCenter = LatLng(6.5244, 3.3792);
const finiteMapCameraConstraint = FiniteMapCameraConstraint();

bool isValidMapCoordinate(double? latitude, double? longitude) {
  return latitude != null &&
      longitude != null &&
      latitude.isFinite &&
      longitude.isFinite &&
      latitude >= -90 &&
      latitude <= 90 &&
      longitude >= -180 &&
      longitude <= 180;
}

LatLng? safeLatLng(double? latitude, double? longitude) {
  if (!isValidMapCoordinate(latitude, longitude)) return null;
  return LatLng(latitude!, longitude!);
}

class FiniteMapCameraConstraint extends CameraConstraint {
  const FiniteMapCameraConstraint();

  @override
  MapCamera? constrain(MapCamera camera) {
    if (!isValidMapCoordinate(
          camera.center.latitude,
          camera.center.longitude,
        ) ||
        !camera.zoom.isFinite ||
        !camera.rotation.isFinite) {
      return null;
    }
    return camera;
  }
}
