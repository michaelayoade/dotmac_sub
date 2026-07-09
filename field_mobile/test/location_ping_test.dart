import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/features/location/location_cadence.dart';
import 'package:dotmac_field/features/location/location_ping_service.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('pingInterval cadence', () {
    test('no pinging off shift or on break', () {
      expect(
        pingInterval(shift: ShiftState.offShift, hasActiveJob: true, moving: true),
        isNull,
      );
      expect(
        pingInterval(shift: ShiftState.onBreak, hasActiveJob: true, moving: true),
        isNull,
      );
    });

    test('tight cadence for active job or movement, relaxed when idle', () {
      expect(
        pingInterval(shift: ShiftState.onShift, hasActiveJob: true, moving: false),
        activePingInterval,
      );
      expect(
        pingInterval(shift: ShiftState.onShift, hasActiveJob: false, moving: true),
        activePingInterval,
      );
      expect(
        pingInterval(shift: ShiftState.onShift, hasActiveJob: false, moving: false),
        idlePingInterval,
      );
    });
  });

  group('LocationPingService capture', () {
    test('does not capture off shift', () async {
      final svc = LocationPingService(
        location: FakeLocation((latitude: 6.5, longitude: 3.3)),
        poster: (_) async => true,
      );
      await svc.captureOnce();
      expect(svc.bufferedCount, 0);
    });

    test('captures a fix on shift with status and work order', () async {
      final svc = LocationPingService(
        location: FakeLocation((latitude: 6.5, longitude: 3.3)),
        poster: (_) async => true,
        clock: () => DateTime.utc(2026, 6, 13, 9, 0, 0),
      )..setShift(ShiftState.onShift);

      await svc.captureOnce(hasActiveJob: true, workOrderId: 'wo-1');
      expect(svc.bufferedCount, 1);
    });

    test('a null fix is skipped without error', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
      )
        ..setShift(ShiftState.onShift);
      await svc.captureOnce();
      expect(svc.bufferedCount, 0);
    });

    test('buffer is bounded, dropping oldest', () async {
      final svc = LocationPingService(
        location: FakeLocation((latitude: 1, longitude: 1)),
        poster: (_) async => true,
        maxBuffer: 3,
      )..setShift(ShiftState.onShift);
      for (var i = 0; i < 5; i++) {
        await svc.captureOnce();
      }
      expect(svc.bufferedCount, 3);
    });
  });

  group('LocationPingService sharing', () {
    test('updateShift calls sharing updater and updates local shift', () async {
      final calls = <({bool enabled, ShiftState shift})>[];
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingUpdater: ({required enabled, required shift}) async {
          calls.add((enabled: enabled, shift: shift));
          return true;
        },
      );

      expect(await svc.updateShift(ShiftState.onShift), isTrue);
      expect(svc.shift, ShiftState.onShift);
      expect(calls.single.enabled, isTrue);
      expect(calls.single.shift, ShiftState.onShift);
    });

    test('updateShift keeps prior shift on sharing failure', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => true,
        sharingUpdater: ({required enabled, required shift}) async => false,
      )..setShift(ShiftState.offShift);

      expect(await svc.updateShift(ShiftState.onShift), isFalse);
      expect(svc.shift, ShiftState.offShift);
    });
  });

  group('LocationPingService flush', () {
    test('clears the buffer on success', () async {
      var posted = 0;
      final svc = LocationPingService(
        location: FakeLocation((latitude: 6.5, longitude: 3.3)),
        poster: (pings) async {
          posted = pings.length;
          return true;
        },
      )..setShift(ShiftState.onShift);
      await svc.captureOnce();
      await svc.captureOnce();
      expect(await svc.flush(), isTrue);
      expect(posted, 2);
      expect(svc.bufferedCount, 0);
    });

    test('retains the buffer on failure', () async {
      final svc = LocationPingService(
        location: FakeLocation((latitude: 6.5, longitude: 3.3)),
        poster: (_) async => false,
      )..setShift(ShiftState.onShift);
      await svc.captureOnce();
      expect(await svc.flush(), isFalse);
      expect(svc.bufferedCount, 1);
    });

    test('flush with empty buffer is a no-op success', () async {
      final svc = LocationPingService(
        location: FakeLocation(null),
        poster: (_) async => false,
      );
      expect(await svc.flush(), isTrue);
    });
  });

  group('background tracking', () {
    test('streamed fixes are buffered and flushed while on shift', () async {
      final fake = FakeLocation((latitude: 6.5, longitude: 3.3));
      var posted = 0;
      final svc = LocationPingService(
        location: fake,
        poster: (pings) async {
          posted += pings.length;
          return true;
        },
      )..setShift(ShiftState.onShift);

      svc.startBackgroundTracking(workOrderId: 'wo-1');
      expect(svc.isBackgroundTracking, isTrue);
      fake.emit((latitude: 6.51, longitude: 3.31));
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(posted, 1);
      expect(svc.bufferedCount, 0); // flushed on success
      await svc.stopBackgroundTracking();
      expect(svc.isBackgroundTracking, isFalse);
    });

    test('streamed fixes are ignored off shift', () async {
      final fake = FakeLocation(null);
      final svc = LocationPingService(location: fake, poster: (_) async => true);
      svc.startBackgroundTracking();
      fake.emit((latitude: 1, longitude: 1));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(svc.bufferedCount, 0);
      await svc.stopBackgroundTracking();
    });

    test('startBackgroundTracking is idempotent', () async {
      final svc = LocationPingService(location: FakeLocation(null), poster: (_) async => true);
      svc.startBackgroundTracking();
      svc.startBackgroundTracking();
      expect(svc.isBackgroundTracking, isTrue);
      await svc.stopBackgroundTracking();
    });
  });
}
