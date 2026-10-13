import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/repositories/support_repository.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:flutter_test/flutter_test.dart';

class _FakeAdapter implements HttpClientAdapter {
  final List<RequestOptions> calls = [];
  int photoStatus = 200;
  String photoType = 'image/png';
  List<int> photoBytes = [137, 80, 78, 71];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add(options);
    if (options.path.contains('/attachments/')) {
      return ResponseBody.fromBytes(photoBytes, photoStatus, headers: {
        Headers.contentTypeHeader: [photoType],
      });
    }
    final isComment = options.path.endsWith('/comments');
    return ResponseBody.fromString(
      isComment
          ? '{"id":"comment-1","ticket_id":"ticket-1",'
              '"body":"Still offline","is_internal":false}'
          : '{"id":"ticket-1","title":"No internet",'
              '"status":"open","priority":"normal"}',
      201,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

SupportRepository _repository(_FakeAdapter adapter) {
  final dio = Dio(
    BaseOptions(
      baseUrl: 'https://test.local/api/v1',
      contentType: Headers.jsonContentType,
    ),
  );
  dio.httpClientAdapter = adapter;
  return SupportRepository(dio);
}

Map<String, String> _fields(RequestOptions request) {
  final form = request.data as FormData;
  return Map<String, String>.fromEntries(form.fields);
}

void main() {
  test('private photo bytes use the authenticated scoped endpoint without URLs',
      () async {
    final adapter = _FakeAdapter();
    final repository = _repository(adapter);
    repository.dio.options.headers['Authorization'] = 'Bearer test-only';
    final bytes = await repository.attachmentImage(
        ticketId: 'ticket-1', fileId: 'file-1');
    expect(bytes, adapter.photoBytes);
    final request = adapter.calls.single;
    expect(request.path, '/me/support/tickets/ticket-1/attachments/file-1');
    expect(request.headers['Authorization'], 'Bearer test-only');
    expect(request.uri.hasQuery, isFalse);
    expect(request.followRedirects, isFalse);
    expect(request.responseType, ResponseType.bytes);
    expect(request.extra['requireFresh'], isTrue);
  });

  test('private photo refuses revoked access, nonimages, and oversized content',
      () async {
    final adapter = _FakeAdapter();
    final repository = _repository(adapter);
    Future<Uint8List> load() =>
        repository.attachmentImage(ticketId: 'ticket-1', fileId: 'file-1');
    adapter.photoStatus = 404;
    await expectLater(load(), throwsA(isA<ApiException>()));
    adapter.photoStatus = 302;
    await expectLater(load(), throwsA(isA<ApiException>()));
    adapter.photoStatus = 200;
    adapter.photoType = 'text/html';
    await expectLater(load(), throwsA(isA<ApiException>()));
    adapter.photoType = 'image/png';
    adapter.photoBytes = List<int>.filled(5 * 1024 * 1024 + 1, 0);
    await expectLater(load(), throwsA(isA<ApiException>()));
  });

  test('caption and selected photo use the ticket comment multipart route',
      () async {
    final directory = await Directory.systemTemp.createTemp('ticket-photo');
    addTearDown(() => directory.delete(recursive: true));
    final photo = File('${directory.path}/repair.png');
    await photo.writeAsBytes([137, 80, 78, 71]);
    final adapter = _FakeAdapter();
    final repository = _repository(adapter);

    await repository.addComment('ticket-1', 'Router light is red',
        attachmentPaths: [photo.path]);

    final request = adapter.calls.single;
    expect(request.method, 'POST');
    expect(request.path, '/me/support/tickets/ticket-1/comments');
    expect(_fields(request), {'body': 'Router light is red'});
    final attachment = (request.data as FormData).files.single;
    expect(attachment.key, 'attachments');
    expect(attachment.value.filename, 'repair.png');
    expect(attachment.value.contentType.toString(), 'image/png');
    expect(attachment.value.length, 4);
  });

  test(
    'ticket creation without attachments still uses multipart form data',
    () async {
      final adapter = _FakeAdapter();
      final repository = _repository(adapter);

      final ticket = await repository.createTicket(
        title: 'No internet',
        description: 'Service has been down since morning',
        priority: 'high',
      );

      expect(ticket.id, 'ticket-1');
      final request = adapter.calls.single;
      expect(request.method, 'POST');
      expect(request.path, '/me/support/tickets');
      expect(request.data, isA<FormData>());
      expect(
        request.contentType,
        startsWith('${Headers.multipartFormDataContentType}; boundary='),
      );
      expect(_fields(request), {
        'title': 'No internet',
        'description': 'Service has been down since morning',
        'priority': 'high',
      });
      expect((request.data as FormData).files, isEmpty);
    },
  );

  test(
    'ticket reply without attachments still uses multipart form data',
    () async {
      final adapter = _FakeAdapter();
      final repository = _repository(adapter);

      final comment = await repository.addComment('ticket-1', 'Still offline');

      expect(comment.id, 'comment-1');
      final request = adapter.calls.single;
      expect(request.method, 'POST');
      expect(request.path, '/me/support/tickets/ticket-1/comments');
      expect(request.data, isA<FormData>());
      expect(
        request.contentType,
        startsWith('${Headers.multipartFormDataContentType}; boundary='),
      );
      expect(_fields(request), {'body': 'Still offline'});
      expect((request.data as FormData).files, isEmpty);
    },
  );
}
