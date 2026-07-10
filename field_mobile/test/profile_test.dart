import 'dart:convert';
import 'dart:ffi';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dotmac_field/core/api/api_client.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/connectivity.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/offline/sync_service.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/execution/execution_controller.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/profile/profile_screen.dart';
import 'package:dotmac_field/features/profile/vendor_profile_provider.dart';
import 'package:drift/drift.dart' hide Column;
import 'package:drift/native.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';

OutboxEntry _entry(
  int seq,
  String ref, {
  String status = 'pending',
  String? error,
}) => OutboxEntry(
  seq: seq,
  clientRef: ref,
  kind: 'transition',
  payloadJson: jsonEncode({'work_order_id': 'wo'}),
  status: status,
  attempts: 1,
  lastError: error,
  createdAt: DateTime.now().toUtc(),
);

class _VendorAuthController extends AuthController {
  @override
  AuthState build() =>
      const Authenticated(LoginMode.vendor, vendorId: 'vendor-1');
}

void main() {
  if (Platform.isLinux) {
    open.overrideFor(
      OperatingSystem.linux,
      () => DynamicLibrary.open('libsqlite3.so.0'),
    );
  }

  late AppDatabase db;
  late SyncService sync;

  setUp(() async {
    db = AppDatabase(NativeDatabase.memory());
    final adapter = FakeHttpAdapter();
    final store = InMemoryTokenStore();
    await store.save(
      accessToken: fakeJwt(
        expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)),
      ),
      refreshToken: 'r',
      loginMode: LoginMode.staff,
    );
    final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
    dio.httpClientAdapter = adapter;
    sync = SyncService(
      db: db,
      api: ApiClient(
        baseUrl: 'https://test.local',
        tokenStore: store,
        dio: dio,
      ),
      connectivity: FakeConnectivity(online: false),
      delay: (_) async {},
    );
  });

  tearDown(() async {
    await sync.dispose();
    await db.close();
  });

  // Drift watch() streams don't settle under the widget tester's FakeAsync
  // zone, so the stream providers are overridden with canned values and the
  // discard path's real DB work runs inside tester.runAsync.
  Widget app({
    List<OutboxEntry> pending = const [],
    List<OutboxEntry> conflicts = const [],
  }) => ProviderScope(
    overrides: [
      syncServiceProvider.overrideWithValue(sync),
      meProvider.overrideWith(
        (ref) async =>
            const MeSummary(name: 'Chidi Tech', openJobs: 2, completedToday: 1),
      ),
      pendingOutboxProvider.overrideWith((ref) => Stream.value(pending)),
      conflictOutboxProvider.overrideWith((ref) => Stream.value(conflicts)),
      pendingPhotosProvider.overrideWith((ref) => Stream.value(0)),
    ],
    child: const MaterialApp(home: ProfileScreen()),
  );

  testWidgets('renders identity and queue counts', (tester) async {
    await tester.pumpWidget(
      app(
        pending: [_entry(1, 'p0'), _entry(2, 'p1'), _entry(3, 'p2')],
        conflicts: [
          _entry(
            4,
            'c0',
            status: 'conflict',
            error: 'Cannot start a job in status completed',
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Chidi Tech'), findsOneWidget);
    expect(find.text('3 queued actions · 0 queued photos'), findsOneWidget);
    expect(find.text('1 need review'), findsOneWidget);
    expect(find.textContaining('Cannot start a job'), findsOneWidget);
  });

  testWidgets('renders vendor profile for vendor sessions', (tester) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          syncServiceProvider.overrideWithValue(sync),
          authControllerProvider.overrideWith(_VendorAuthController.new),
          vendorProfileProvider.overrideWith(
            (ref) async => const VendorProfile(
              name: 'Miracle David',
              vendorName: 'Miracle Racheal David',
              vendorRole: 'vendors',
            ),
          ),
          meProvider.overrideWith(
            (ref) async => const MeSummary(
              name: 'Previous Tech',
              openJobs: 9,
              completedToday: 4,
            ),
          ),
          pendingOutboxProvider.overrideWith((ref) => Stream.value([])),
          conflictOutboxProvider.overrideWith((ref) => Stream.value([])),
          pendingPhotosProvider.overrideWith((ref) => Stream.value(0)),
        ],
        child: const MaterialApp(home: ProfileScreen()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Miracle David'), findsOneWidget);
    expect(find.text('Miracle Racheal David · vendors'), findsOneWidget);
    expect(find.text('Previous Tech'), findsNothing);
  });

  testWidgets('discard removes the conflict row after confirmation', (
    tester,
  ) async {
    late OutboxEntry seeded;
    await tester.runAsync(() async {
      await db
          .into(db.outboxEntries)
          .insert(
            OutboxEntriesCompanion.insert(
              clientRef: 'c0',
              kind: 'transition',
              payloadJson: jsonEncode({'work_order_id': 'wo'}),
              status: const Value('conflict'),
              lastError: const Value('rejected'),
              createdAt: DateTime.now().toUtc(),
            ),
          );
      seeded = (await db.select(db.outboxEntries).get()).single;
    });

    await tester.pumpWidget(app(conflicts: [seeded]));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(Key('discard-${seeded.clientRef}')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Discard'));
    await tester.pumpAndSettle();

    await tester.runAsync(() async {
      expect(await db.select(db.outboxEntries).get(), isEmpty);
    });
  });

  testWidgets('keep leaves the conflict in place', (tester) async {
    late OutboxEntry seeded;
    await tester.runAsync(() async {
      await db
          .into(db.outboxEntries)
          .insert(
            OutboxEntriesCompanion.insert(
              clientRef: 'c1',
              kind: 'transition',
              payloadJson: jsonEncode({'work_order_id': 'wo'}),
              status: const Value('conflict'),
              createdAt: DateTime.now().toUtc(),
            ),
          );
      seeded = (await db.select(db.outboxEntries).get()).single;
    });

    await tester.pumpWidget(app(conflicts: [seeded]));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(Key('discard-${seeded.clientRef}')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Keep'));
    await tester.pumpAndSettle();

    await tester.runAsync(() async {
      expect((await db.select(db.outboxEntries).get()).length, 1);
    });
  });
}
