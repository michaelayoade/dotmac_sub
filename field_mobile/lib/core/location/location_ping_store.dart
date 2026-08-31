import 'dart:convert';
import 'dart:io';

import '../../features/location/location_cadence.dart';
import '../secure/evidence_cipher.dart';
import '../secure/evidence_files.dart';

class LocationPingPayload {
  const LocationPingPayload({
    required this.latitude,
    required this.longitude,
    required this.capturedAt,
    required this.shift,
    this.workOrderId,
  });

  final double latitude;
  final double longitude;
  final DateTime capturedAt;
  final ShiftState shift;
  final String? workOrderId;

  Map<String, dynamic> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'captured_at': capturedAt.toUtc().toIso8601String(),
    'status': shift.apiValue,
    'crm_work_order_id': ?workOrderId,
  };

  factory LocationPingPayload.fromJson(Map<String, dynamic> json) {
    final latitude = (json['latitude'] as num?)?.toDouble();
    final longitude = (json['longitude'] as num?)?.toDouble();
    final capturedAt = DateTime.tryParse(json['captured_at']?.toString() ?? '');
    final shift = ShiftStateApi.fromApiValue(json['status']?.toString() ?? '');
    if (latitude == null ||
        longitude == null ||
        capturedAt == null ||
        shift == null) {
      throw const FormatException('Invalid persisted location ping');
    }
    return LocationPingPayload(
      latitude: latitude,
      longitude: longitude,
      capturedAt: capturedAt.toUtc(),
      shift: shift,
      workOrderId: json['crm_work_order_id']?.toString(),
    );
  }
}

abstract class LocationPingStore {
  Future<List<LocationPingPayload>> load();

  Future<void> save(List<LocationPingPayload> pings);
}

class MemoryLocationPingStore implements LocationPingStore {
  List<LocationPingPayload> _pings = const [];

  @override
  Future<List<LocationPingPayload>> load() async => List.of(_pings);

  @override
  Future<void> save(List<LocationPingPayload> pings) async {
    _pings = List.of(pings);
  }
}

/// The queue of pings the device has recorded but not yet delivered. Each ping
/// is a customer premises the technician stood at, so the file on disk is an
/// AES-GCM envelope bound to the scope, never readable JSON. There is no
/// plaintext fallback: [cipher] is required, because a queue we cannot encrypt
/// is a queue we must not write.
class FileLocationPingStore implements LocationPingStore {
  FileLocationPingStore(
    this.file, {
    required this.cipher,
    required this.scopeKey,
    DateTime Function()? clock,
  }) : _clock = clock ?? (() => DateTime.now().toUtc());

  final File file;
  final EvidenceCipher cipher;
  final String scopeKey;
  final DateTime Function() _clock;

  String get _context => evidenceContext(scopeKey, 'location', 'queue');

  @override
  Future<List<LocationPingPayload>> load() async {
    if (!await file.exists()) return const [];
    try {
      final envelope = await file.readAsBytes();
      final decoded = jsonDecode(
        utf8.decode(cipher.open(envelope, context: _context)),
      );
      if (decoded is! List) {
        throw const FormatException('Persisted location queue is not a list');
      }
      return [
        for (final item in decoded)
          if (item is Map)
            LocationPingPayload.fromJson(item.cast<String, dynamic>()),
      ];
    } on Object {
      // Includes an envelope belonging to a key we no longer hold: unreadable
      // is treated exactly like corrupt, and neither is ever surfaced.
      await _quarantineCorruptQueue();
      return const [];
    }
  }

  Future<void> _quarantineCorruptQueue() async {
    final marker = File('${file.path}.corrupt');
    await marker.writeAsString(
      'discarded_at=${_clock().toIso8601String()}\n',
      flush: true,
    );
    if (await file.exists()) await file.delete();
  }

  @override
  Future<void> save(List<LocationPingPayload> pings) async {
    await file.parent.create(recursive: true);
    if (pings.isEmpty) {
      if (await file.exists()) await file.delete();
      return;
    }
    final temporary = File('${file.path}.tmp');
    await temporary.writeAsBytes(
      cipher.seal(
        utf8.encode(jsonEncode(pings.map((ping) => ping.toJson()).toList())),
        context: _context,
      ),
      flush: true,
    );
    if (await file.exists()) await file.delete();
    await temporary.rename(file.path);
  }
}
