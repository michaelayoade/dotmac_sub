import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';

/// Connectivity abstraction so the sync engine is testable with a fake.
abstract class ConnectivitySource {
  Stream<bool> get onlineChanges;
  Future<bool> get isOnline;
}

class DeviceConnectivity implements ConnectivitySource {
  final _plugin = Connectivity();

  bool _has(List<ConnectivityResult> results) =>
      results.any((r) => r != ConnectivityResult.none);

  @override
  Stream<bool> get onlineChanges => _plugin.onConnectivityChanged.map(_has).distinct();

  @override
  Future<bool> get isOnline async => _has(await _plugin.checkConnectivity());
}

class FakeConnectivity implements ConnectivitySource {
  FakeConnectivity({bool online = true}) {
    _online = online;
  }

  late bool _online;
  final _controller = StreamController<bool>.broadcast();

  set online(bool value) {
    _online = value;
    _controller.add(value);
  }

  @override
  Stream<bool> get onlineChanges => _controller.stream;

  @override
  Future<bool> get isOnline async => _online;
}
