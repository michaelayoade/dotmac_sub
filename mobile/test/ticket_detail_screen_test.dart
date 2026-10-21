import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/app.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/features/support/ticket_detail_screen.dart';
import 'package:dotmac_portal/src/models/page.dart' as models;
import 'package:dotmac_portal/src/models/ticket.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/providers/ticket_conversation_controller.dart';
import 'package:dotmac_portal/src/repositories/app_realtime_socket.dart';
import 'package:dotmac_portal/src/repositories/support_repository.dart';
import 'package:dotmac_portal/src/widgets/attachment_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

const _ticketId = 'e94a93e9-bdf0-49e7-a300-9d5226ba86b7';

TicketComment _comment(
  String id,
  TicketCommentAuthorType authorType,
  String body, {
  bool isInternal = false,
}) =>
    TicketComment(
      id: id,
      ticketId: _ticketId,
      authorType: authorType,
      body: body,
      isInternal: isInternal,
      createdAt: DateTime.utc(2026, 9, 14, 12),
    );

class _FakeSupportRepository extends SupportRepository {
  _FakeSupportRepository(this.currentComments) : super(Dio());

  List<TicketComment> currentComments;
  Object? commentsError;
  int commentReads = 0;
  final List<({String ticketId, String body, List<String>? paths})> sent = [];
  final List<({String ticketId, String fileId})> photoReads = [];
  bool photoDenied = false;

  @override
  Future<Uint8List> attachmentImage(
      {required String ticketId, required String fileId}) async {
    photoReads.add((ticketId: ticketId, fileId: fileId));
    if (photoDenied) throw ApiException('Not found', statusCode: 404);
    return base64Decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=');
  }

  @override
  Future<TicketComment> addComment(
    String ticketId,
    String body, {
    List<String>? attachmentPaths,
  }) async {
    sent.add((ticketId: ticketId, body: body, paths: attachmentPaths));
    final comment = _comment('sent', TicketCommentAuthorType.customer, body);
    currentComments = [...currentComments, comment];
    return comment;
  }

  @override
  Future<Ticket> ticket(String id) async => Ticket(
        id: id,
        title: 'Test ticket',
        status: 'open',
        priority: 'normal',
        number: 'TKT-100',
        createdAt: DateTime.utc(2026, 9, 14, 11),
      );

  @override
  Future<models.Page<TicketComment>> comments(
    String ticketId, {
    int limit = 100,
    int offset = 0,
  }) async {
    commentReads += 1;
    final error = commentsError;
    if (error != null) throw error;
    return models.Page(
      items: List.of(currentComments),
      count: currentComments.length,
      limit: limit,
      offset: offset,
    );
  }
}

class _IdleRealtimeSocket implements AppRealtimeSocket {
  @override
  Stream<AppRealtimeSignal> get signals => const Stream.empty();

  @override
  Future<void> close() async {}

  @override
  Future<void> connect() async {}

  @override
  Future<void> dispose() async {}
}

class _FailingRealtimeSocket extends _IdleRealtimeSocket {
  @override
  Future<void> connect() => Future.error(StateError('offline'));
}

Widget _app(
  _FakeSupportRepository repository, {
  AppRealtimeSocketFactory socketFactory = _IdleRealtimeSocket.new,
  TicketConversationTiming timing = const TicketConversationTiming(),
}) =>
    ProviderScope(
      overrides: [
        supportRepositoryProvider.overrideWithValue(repository),
        appRealtimeSocketFactoryProvider.overrideWithValue(socketFactory),
        ticketConversationTimingProvider.overrideWithValue(timing),
      ],
      child: MaterialApp(
        theme: dotmacThemeFor(Brightness.light),
        home: const TicketDetailScreen(ticketId: _ticketId),
      ),
    );

void main() {
  test('native attachment metadata uses file identity and ignores storage keys',
      () {
    final attachment = TicketAttachment.fromJson({
      'stored_file_id': 'file-1',
      'file_name': 'repair.png',
      'content_type': 'image/png',
      'file_size': 123,
      'storage_key': 'private/not-a-download-url',
    });
    expect(attachment.id, 'file-1');
    expect(attachment.storedFileId, 'file-1');
    expect(attachment.filename, 'repair.png');
    expect(attachment.size, 123);
    expect(attachment.url, isNull);
    final legacy = TicketAttachment.fromJson(
        {'id': 'old', 'name': 'legacy.pdf', 'size': 12});
    expect(legacy.filename, 'legacy.pdf');
    expect(legacy.size, 12);
    expect(legacy.storedFileId, isNull);
  });

  testWidgets(
      'native photo previews privately, retries denial, and rechecks on reopen',
      (tester) async {
    final attachment = TicketAttachment.fromJson({
      'stored_file_id': 'file-1',
      'file_name': 'repair.png',
      'content_type': 'image/png',
      'file_size': 68,
      'storage_key': 'private/never-used',
    });
    final repository = _FakeSupportRepository([
      TicketComment(
          id: 'photo-comment',
          ticketId: _ticketId,
          authorType: TicketCommentAuthorType.customer,
          body: 'Router light',
          attachments: [attachment]),
    ])
      ..photoDenied = true;
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();
    await tester.tap(find.text('repair.png'));
    await tester.pumpAndSettle();
    expect(find.text('Could not load this photo. Please try again.'),
        findsOneWidget);
    expect(find.byType(Image), findsNothing);
    repository.photoDenied = false;
    await tester.tap(find.text('Retry'));
    await tester.pumpAndSettle();
    expect(tester.widget<Image>(find.byType(Image)).image, isA<MemoryImage>());
    expect(repository.photoReads, hasLength(2));
    expect(
        repository.photoReads.first, (ticketId: _ticketId, fileId: 'file-1'));
    await tester.pageBack();
    await tester.pumpAndSettle();
    repository.photoDenied = true;
    await tester.tap(find.text('repair.png'));
    await tester.pumpAndSettle();
    expect(repository.photoReads, hasLength(3));
    expect(find.byType(Image), findsNothing);
    expect(find.text('Could not load this photo. Please try again.'),
        findsOneWidget);
  });

  testWidgets('legacy private links stay visible without external access',
      (tester) async {
    final repository = _FakeSupportRepository([
      TicketComment(
        id: 'legacy-photo',
        ticketId: _ticketId,
        authorType: TicketCommentAuthorType.customer,
        body: 'Legacy attachment',
        attachments: [
          TicketAttachment.fromJson({
            'id': 'old-file',
            'name': 'legacy.png',
            'content_type': 'image/png',
            'url': '/api/v1/me/support/tickets/private/attachments/old-file',
          })
        ],
      ),
    ]);
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();
    expect(find.text('legacy.png'), findsOneWidget);
    expect(
        tester.widget<ActionChip>(find.byType(ActionChip)).onPressed, isNull);
    expect(find.byType(Image), findsNothing);
    expect(repository.photoReads, isEmpty);
  });

  testWidgets(
      'photo reply needs a caption and retains the draft until supplied',
      (tester) async {
    final directory = Directory.systemTemp.createTempSync('ticket-caption');
    final photo = File('${directory.path}/repair.png');
    photo.writeAsBytesSync(base64Decode(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=',
    ));
    const pickerChannel = MethodChannel('plugins.flutter.io/image_picker');
    const storageChannel =
        MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
    tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(storageChannel, (_) async => null);
    tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(pickerChannel, (call) async {
      expect(call.method, 'pickImage');
      return photo.path;
    });
    addTearDown(() {
      tester.binding.defaultBinaryMessenger
          .setMockMethodCallHandler(pickerChannel, null);
      tester.binding.defaultBinaryMessenger
          .setMockMethodCallHandler(storageChannel, null);
      directory.deleteSync(recursive: true);
    });
    final repository = _FakeSupportRepository([]);
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Attach photo'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Choose from gallery'));
    await tester.pumpAndSettle();
    await tester.runAsync(() async {
      // Let the picker's real file-size read finish outside the fake clock.
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pumpAndSettle();

    expect(
        tester
            .widget<AttachmentPicker>(find.byType(AttachmentPicker))
            .attachments
            .single
            .path,
        photo.path);
    await tester.enterText(find.byType(TextField), '   ');
    await tester.tap(find.byTooltip('Send reply'));
    await tester.pump();
    expect(repository.sent, isEmpty);
    expect(find.text('Add a message or caption before sending your photo.'),
        findsOneWidget);
    expect(tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '   ');
    expect(
        tester
            .widget<AttachmentPicker>(find.byType(AttachmentPicker))
            .attachments
            .single
            .path,
        photo.path);

    await tester.enterText(find.byType(TextField), '  Router light is red  ');
    await tester.tap(find.byTooltip('Send reply'));
    await tester.pumpAndSettle();
    expect(repository.sent.single.ticketId, _ticketId);
    expect(repository.sent.single.body, 'Router light is red');
    expect(repository.sent.single.paths, [photo.path]);
    expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text, '');
    expect(find.byType(AttachmentPicker), findsNothing);
  });

  testWidgets('ticket replies identify customer, staff, and system senders',
      (tester) async {
    final repository = _FakeSupportRepository([
      _comment('customer', TicketCommentAuthorType.customer, 'My reply'),
      _comment('staff', TicketCommentAuthorType.staff, 'Support reply'),
      _comment('system', TicketCommentAuthorType.system, 'Service notice'),
      _comment('unknown', TicketCommentAuthorType.unknown, 'Legacy sender'),
      _comment(
        'internal',
        TicketCommentAuthorType.staff,
        'Private staff note',
        isInternal: true,
      ),
    ]);

    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    expect(find.text('You'), findsOneWidget);
    expect(find.text('Support Team'), findsOneWidget);
    expect(find.text('Service update'), findsOneWidget);
    expect(find.text('Sender unavailable'), findsOneWidget);
    expect(find.text('Private staff note'), findsNothing);

    Align alignmentFor(String id) => tester.widget<Align>(
          find.byKey(ValueKey('ticket-comment-$id')),
        );
    expect(alignmentFor('customer').alignment, Alignment.centerRight);
    expect(alignmentFor('staff').alignment, Alignment.centerLeft);
    expect(alignmentFor('system').alignment, Alignment.center);

    expect(
      find.bySemanticsLabel(RegExp(r'^Message from Support Team')),
      findsOneWidget,
    );
  });

  testWidgets('manual refresh displays a new public staff reply',
      (tester) async {
    final repository = _FakeSupportRepository([
      _comment('customer', TicketCommentAuthorType.customer, 'Any update?'),
    ]);
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();
    expect(repository.commentReads, 1);

    repository.currentComments = [
      ...repository.currentComments,
      _comment('staff', TicketCommentAuthorType.staff, 'We fixed the line.'),
    ];
    await tester.tap(find.byTooltip('Refresh ticket'));
    await tester.pumpAndSettle();

    expect(repository.commentReads, 2);
    expect(find.text('We fixed the line.'), findsOneWidget);
    expect(find.text('Support Team'), findsOneWidget);
  });

  testWidgets('refresh failure retains the last conversation and offers retry',
      (tester) async {
    final repository = _FakeSupportRepository([
      _comment('customer', TicketCommentAuthorType.customer, 'Still visible'),
    ]);
    await tester.pumpWidget(_app(repository));
    await tester.pumpAndSettle();

    repository.commentsError = ApiException('Temporary network problem');
    await tester.tap(find.byTooltip('Refresh ticket'));
    await tester.pumpAndSettle();

    expect(find.text('Still visible'), findsOneWidget);
    expect(
      find.text('Could not refresh replies. Showing the last update.'),
      findsOneWidget,
    );
    expect(find.text('Retry'), findsOneWidget);
  });

  testWidgets('prolonged socket failure shows the manual-refresh fallback', (
    tester,
  ) async {
    final repository = _FakeSupportRepository([]);
    await tester.pumpWidget(
      _app(
        repository,
        socketFactory: _FailingRealtimeSocket.new,
        timing: const TicketConversationTiming(
          unavailableAfter: Duration(milliseconds: 10),
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 11));

    expect(
      find.text('Live comment updates unavailable. Pull to refresh.'),
      findsOneWidget,
    );
  });
}
