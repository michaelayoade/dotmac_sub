import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/providers/read_notifications.dart';
import 'package:dotmac_portal/src/repositories/notification_repository.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.onFetch);

  final ResponseBody Function(RequestOptions options) onFetch;
  final List<RequestOptions> calls = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add(options);
    return onFetch(options);
  }

  @override
  void close({bool force = false}) {}
}

class _FakeLegacyStore implements LegacyNotificationReadStore {
  _FakeLegacyStore(this.ids);

  final List<String> ids;
  bool cleared = false;

  @override
  Future<void> clear() async => cleared = true;

  @override
  Future<List<String>> readIds() async => ids;
}

Dio _dio(_FakeAdapter adapter) {
  final dio = Dio(BaseOptions(baseUrl: 'https://test.local/api/v1'));
  dio.httpClientAdapter = adapter;
  return dio;
}

ResponseBody _json(String body, [int status = 200]) =>
    ResponseBody.fromString(body, status, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType],
    });

void main() {
  test('notification list parses server-owned read state', () async {
    final adapter = _FakeAdapter(
      (_) => _json(
        '{"items":[{"id":"n-1","channel":"push",'
        '"status":"delivered","is_read":true}],'
        '"count":1,"limit":50,"offset":0}',
      ),
    );

    final page = await NotificationRepository(_dio(adapter)).list();

    expect(page.items.single.isRead, isTrue);
  });

  test('markRead posts selected IDs to the self-scoped owner', () async {
    final adapter = _FakeAdapter((_) => _json('{"marked":2}'));
    final repo = NotificationRepository(_dio(adapter));

    final marked = await repo.markRead(['n-1', 'n-2']);

    expect(marked, 2);
    expect(adapter.calls, hasLength(1));
    expect(adapter.calls.single.method, 'POST');
    expect(adapter.calls.single.path, '/me/notifications/read');
    expect(adapter.calls.single.data, {
      'notification_ids': ['n-1', 'n-2'],
      'all_visible': false,
    });
  });

  test('markAllRead asks the server to resolve the visible inbox', () async {
    final adapter = _FakeAdapter((_) => _json('{"marked":4}'));
    final repo = NotificationRepository(_dio(adapter));

    final marked = await repo.markAllRead();

    expect(marked, 4);
    expect(adapter.calls.single.data, {
      'notification_ids': <String>[],
      'all_visible': true,
    });
  });

  test('legacy migration clears device IDs only after server acceptance',
      () async {
    final adapter = _FakeAdapter((_) => _json('{"marked":1}'));
    final store = _FakeLegacyStore(['old-read-id']);

    final migrated = await NotificationReadMigration(
      repository: NotificationRepository(_dio(adapter)),
      store: store,
    ).run();

    expect(migrated, isTrue);
    expect(store.cleared, isTrue);
    expect(adapter.calls.single.data, {
      'notification_ids': ['old-read-id'],
      'all_visible': false,
    });
  });

  test('legacy migration keeps device IDs when the server rejects them',
      () async {
    final adapter = _FakeAdapter((options) {
      throw DioException(
        requestOptions: options,
        response: Response(
          requestOptions: options,
          statusCode: 503,
          data: {'detail': 'unavailable'},
        ),
        type: DioExceptionType.badResponse,
      );
    });
    final store = _FakeLegacyStore(['retry-later']);

    await expectLater(
      NotificationReadMigration(
        repository: NotificationRepository(_dio(adapter)),
        store: store,
      ).run(),
      throwsA(isA<ApiException>()),
    );

    expect(store.cleared, isFalse);
  });
}
