import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/features/profile/field_visit_chat_screen.dart';
import 'package:dotmac_portal/src/models/field_visit_chat.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:dotmac_portal/src/repositories/work_order_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _Repository extends WorkOrderRepository {
  _Repository() : super(Dio());
  bool canSend = true;
  bool failRead = false;
  bool failSend = false;
  int reads = 0;
  int sends = 0;
  final requestIds = <String?>[];

  @override
  Future<FieldVisitChat> chat(String workOrderId) async {
    reads++;
    if (failRead) throw Exception('Offline');
    return FieldVisitChat(available: true, canSend: canSend);
  }

  @override
  Future<FieldVisitMessage> sendChatMessage(String id,
      {required String body, String? requestId}) async {
    sends++;
    requestIds.add(requestId);
    if (failSend) throw Exception('Unconfirmed');
    return FieldVisitMessage(
        id: 'm1',
        body: body,
        fromCustomer: true,
        createdAt: DateTime.utc(2026));
  }
}

Future<void> _open(WidgetTester tester, _Repository repository) async {
  await tester.pumpWidget(ProviderScope(
    overrides: [workOrderRepositoryProvider.overrideWithValue(repository)],
    child: const MaterialApp(home: FieldVisitChatScreen(workOrderId: 'sub-1')),
  ));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('closed history disables sending', (tester) async {
    final repository = _Repository()..canSend = false;
    await _open(tester, repository);
    final button = tester.widget<IconButton>(find.byWidgetPredicate(
        (widget) => widget is IconButton && widget.tooltip == 'Send message'));
    expect(button.onPressed, isNull);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('uncertain send retains draft and never retries itself',
      (tester) async {
    final repository = _Repository()..failSend = true;
    await _open(tester, repository);
    await tester.enterText(find.byType(TextField), 'Please use side gate');
    await tester.tap(find.byTooltip('Send message'));
    await tester.pumpAndSettle();
    expect(repository.sends, 1);
    expect(find.text('Please use side gate'), findsOneWidget);
    expect(
        find.textContaining('Could not confirm your message'), findsOneWidget);
    await tester.pump(const Duration(seconds: 21));
    await tester.pumpAndSettle();
    expect(repository.sends, 1);
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets(
      'manual retry retains identity and edited draft creates a new identity',
      (tester) async {
    final repository = _Repository()..failSend = true;
    await _open(tester, repository);
    await tester.enterText(find.byType(TextField), 'Use side gate');
    await tester.tap(find.byTooltip('Send message'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Send message'));
    await tester.pumpAndSettle();
    expect(repository.requestIds.first, isNotNull);
    expect(repository.requestIds[1], repository.requestIds.first);
    await tester.enterText(find.byType(TextField), 'Use front gate');
    await tester.tap(find.byTooltip('Send message'));
    await tester.pumpAndSettle();
    expect(repository.requestIds[2], isNot(repository.requestIds.first));
    await tester.pumpWidget(const SizedBox());
  });

  testWidgets('failed refresh disables send and disposal cancels polling',
      (tester) async {
    final repository = _Repository();
    await _open(tester, repository);
    repository.failRead = true;
    await tester.tap(find.byTooltip('Refresh conversation'));
    await tester.pumpAndSettle();
    expect(
        tester
            .widget<IconButton>(find.byWidgetPredicate((widget) =>
                widget is IconButton && widget.tooltip == 'Send message'))
            .onPressed,
        isNull);
    await tester.pumpWidget(const SizedBox());
    final reads = repository.reads;
    await tester.pump(const Duration(seconds: 40));
    expect(repository.reads, reads);
  });
}
