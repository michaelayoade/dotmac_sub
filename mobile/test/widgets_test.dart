import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/widgets/async_value_view.dart';
import 'package:dotmac_portal/src/widgets/status_chip.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  group('StatusChip', () {
    testWidgets('renders the label with underscores as spaces', (tester) async {
      await tester.pumpWidget(_wrap(const StatusChip('on_hold')));
      expect(find.text('on hold'), findsOneWidget);
    });

    testWidgets('invoice factory maps status to a label', (tester) async {
      await tester.pumpWidget(_wrap(StatusChip.forInvoice('paid')));
      expect(find.text('paid'), findsOneWidget);
    });
  });

  group('AsyncValueView', () {
    testWidgets('shows a spinner while loading', (tester) async {
      await tester.pumpWidget(_wrap(
        AsyncValueView<int>(
          value: const AsyncValue.loading(),
          data: (v) => Text('value $v'),
        ),
      ));
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });

    testWidgets('shows the error message and a Retry button', (tester) async {
      var retried = false;
      await tester.pumpWidget(_wrap(
        AsyncValueView<int>(
          value: const AsyncValue<int>.error('boom', StackTrace.empty),
          onRetry: () => retried = true,
          data: (v) => Text('value $v'),
        ),
      ));
      expect(find.textContaining('boom'), findsOneWidget);
      expect(find.text('Retry'), findsOneWidget);
      await tester.tap(find.text('Retry'));
      expect(retried, isTrue);
    });

    testWidgets('renders data when available', (tester) async {
      await tester.pumpWidget(_wrap(
        AsyncValueView<int>(
          value: const AsyncValue.data(42),
          data: (v) => Text('value $v'),
        ),
      ));
      expect(find.text('value 42'), findsOneWidget);
    });
  });

  group('EmptyState', () {
    testWidgets('renders the icon and message', (tester) async {
      await tester.pumpWidget(_wrap(
        const EmptyState(icon: Icons.inbox_outlined, message: 'Nothing here'),
      ));
      expect(find.text('Nothing here'), findsOneWidget);
      expect(find.byIcon(Icons.inbox_outlined), findsOneWidget);
    });
  });
}
