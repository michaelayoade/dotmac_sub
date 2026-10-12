import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/features/profile/technician_profile_screen.dart';
import 'package:dotmac_portal/src/models/field_identity.dart';
import 'package:dotmac_portal/src/providers/field_identity_provider.dart';
import 'package:dotmac_portal/src/repositories/field_identity_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

const job = '11111111-1111-1111-1111-111111111111';
ApprovedFieldIdentity approved(
        [String generation = '22222222-2222-2222-2222-222222222222']) =>
    ApprovedFieldIdentity(
        displayName: 'Synthetic engineer',
        companyName: 'Synthetic company',
        reviewedAt: DateTime.utc(2026, 10, 11),
        portraitRevision: 1,
        assignmentGeneration: generation);

class _Repository extends FieldIdentityRepository {
  _Repository() : super(Dio());
  FieldIdentityResult result =
      FieldIdentityResult(workOrderId: job, approved: approved());
  Object? error;
  Uint8List? photoBytes;
  Completer<FieldIdentityResult>? pending;
  bool reassign = false;
  int cards = 0, photos = 0;
  @override
  Future<FieldIdentityResult> card(String id) async {
    cards++;
    if (error != null) throw error!;
    if (pending != null) return pending!.future;
    if (reassign && cards > 1) {
      return FieldIdentityResult(
          workOrderId: job,
          approved: approved('33333333-3333-3333-3333-333333333333'));
    }
    return result;
  }

  @override
  Future<Uint8List> portrait(String id, ApprovedFieldIdentity identity) async {
    photos++;
    if (photoBytes != null) return photoBytes!;
    return base64Decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGPkCqhgYGBgYgADAAmUANYvw9QsAAAAAElFTkSuQmCC');
  }
}

Future<void> _show(WidgetTester tester, _Repository repo) async {
  await tester.pumpWidget(ProviderScope(
      overrides: [fieldIdentityRepositoryProvider.overrideWithValue(repo)],
      child:
          const MaterialApp(home: TechnicianProfileScreen(workOrderId: job))));
  await _settle(tester);
}

Future<void> _settle(WidgetTester tester) async {
  await tester.pump(const Duration(milliseconds: 500));
  // The explicit native image decoder completes outside Flutter's fake clock.
  for (var i = 0; i < 3; i++) {
    await tester.pump();
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 30)));
  }
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('fresh reviewed card and authenticated photo appear together',
      (tester) async {
    final repo = _Repository();
    await _show(tester, repo);
    expect(find.text('Identity reviewed'), findsOneWidget);
    expect(find.text('Synthetic engineer'), findsOneWidget);
    expect(find.byType(Image), findsOneWidget);
    expect(repo.cards, 2);
    expect(repo.photos, 1);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets('unselected vendor crew never requests photo or shows badge',
      (tester) async {
    final repo = _Repository()
      ..result = const FieldIdentityResult(
          workOrderId: job,
          unavailable: FieldIdentityUnavailable.crewNotSelected);
    await _show(tester, repo);
    expect(find.textContaining('has not selected'), findsOneWidget);
    expect(find.text('Identity reviewed'), findsNothing);
    expect(repo.photos, 0);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets(
      'refresh removes approval immediately and network error keeps it hidden',
      (tester) async {
    final repo = _Repository();
    await _show(tester, repo);
    repo.pending = Completer<FieldIdentityResult>();
    await tester.tap(find.byTooltip('Refresh technician'));
    await tester.pump();
    expect(find.text('Identity reviewed'), findsNothing);
    expect(find.byType(Image), findsNothing);
    repo.pending!.completeError(StateError('offline'));
    await _settle(tester);
    expect(find.textContaining('could not confirm'), findsOneWidget);
    expect(find.text('Identity reviewed'), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets('revoked response removes prior photo and badge', (tester) async {
    final repo = _Repository();
    await _show(tester, repo);
    repo.result = const FieldIdentityResult(
        workOrderId: job, unavailable: FieldIdentityUnavailable.notApproved);
    await tester.tap(find.byTooltip('Refresh technician'));
    await _settle(tester);
    expect(find.text('Identity reviewed'), findsNothing);
    expect(find.byType(Image), findsNothing);
    expect(repo.photos, 1);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets(
      'reassignment during portrait read shows no approval or old photo',
      (tester) async {
    final repo = _Repository()..reassign = true;
    await _show(tester, repo);
    expect(find.text('Identity reviewed'), findsNothing);
    expect(find.byType(Image), findsNothing);
    expect(find.textContaining('could not confirm'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets(
      'background clears approval, resume refetches, disposed screen stops polling',
      (tester) async {
    final repo = _Repository();
    await _show(tester, repo);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await tester.pump();
    expect(find.text('Identity reviewed'), findsNothing);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    final calls = repo.cards;
    await tester.pump(const Duration(seconds: 40));
    expect(repo.cards, calls);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await _settle(tester);
    expect(repo.cards, greaterThan(calls));
    await tester.pumpWidget(const SizedBox());
    final disposedCalls = repo.cards;
    await tester.pump(const Duration(seconds: 40));
    expect(repo.cards, disposedCalls);
  });
  testWidgets('covered route stops polling and returning refetches approval',
      (tester) async {
    final repo = _Repository();
    await _show(tester, repo);
    final navigator = tester.state<NavigatorState>(find.byType(Navigator));
    unawaited(navigator.push(MaterialPageRoute<void>(
        builder: (_) => const Scaffold(body: Text('Other route')))));
    await _settle(tester);
    final calls = repo.cards;
    await tester.pump(const Duration(seconds: 40));
    expect(repo.cards, calls);
    navigator.pop();
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 100));
      await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 30)));
    }
    expect(find.text('Identity reviewed'), findsOneWidget);
    expect(repo.cards, greaterThan(calls));
    await tester.pumpWidget(const SizedBox());
  });
  testWidgets('invalid portrait bytes never show approval badge',
      (tester) async {
    final repo = _Repository()..photoBytes = Uint8List.fromList([1, 2, 3]);
    await _show(tester, repo);
    expect(find.text('Identity reviewed'), findsNothing);
    expect(find.byType(Image), findsNothing);
    await tester.pumpWidget(const SizedBox());
  });
}
