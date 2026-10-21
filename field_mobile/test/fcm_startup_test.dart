import 'dart:async';
import 'package:dotmac_field/core/push/fcm_push_source.dart';
import 'package:dotmac_field/core/push/push_source.dart';
import 'package:firebase_core_platform_interface/test.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setupFirebaseCoreMocks();
  test('pending push calls cannot block startup or lose a cold tap', () async {
    final initial = Completer<Map<String, Object?>>();
    final permission = Completer<Map<String, int>>();
    final calls = <String>[];
    const channel = MethodChannel('plugins.flutter.io/firebase_messaging');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
          calls.add(call.method);
          if (call.method == 'Messaging#getInitialMessage') {
            return initial.future;
          }
          if (call.method == 'Messaging#requestPermission') {
            return permission.future;
          }
          return null;
        });
    final source = await FcmPushSource.tryCreate().timeout(
      const Duration(seconds: 2),
    );
    expect(source, isNotNull);
    expect(calls, isEmpty);
    final messages = <PushMessage>[];
    final subscription = source!.messages.listen(messages.add);
    await Future<void>.delayed(Duration.zero);
    expect(calls, contains('Messaging#getInitialMessage'));
    expect(calls, contains('Messaging#requestPermission'));
    initial.complete({
      'messageId': 'cold-launch',
      'data': {'work_order_id': 'test-job'},
    });
    await Future<void>.delayed(Duration.zero);
    expect(messages.single.fromTap, isTrue);
    expect(messages.single.data['work_order_id'], 'test-job');
    permission.complete({'authorizationStatus': 1});
    await subscription.cancel();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });
}
