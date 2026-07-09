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
import 'package:dotmac_field/features/vendor/trace_recorder.dart';
import 'package:dotmac_field/features/vendor/vendor_providers.dart';
import 'package:drift/native.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

import 'helpers/fake_http.dart';

void main() {
  if (Platform.isLinux) {
    open.overrideFor(OperatingSystem.linux, () => DynamicLibrary.open('libsqlite3.so.0'));
  }

  group('trace recorder', () {
    test('accumulates points, filters jitter, computes distance', () {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.4281, longitude: 3.4216));
      recorder.addPoint((latitude: 6.4281, longitude: 3.4216)); // jitter: dropped
      recorder.addPoint((latitude: 6.4290, longitude: 3.4216)); // ~100 m north
      recorder.stop();

      expect(recorder.points.length, 2);
      expect(recorder.distanceMeters, closeTo(100, 5));
      expect(recorder.hasUsableTrace, isTrue);
    });

    test('geojson is a LineString of lng,lat pairs', () {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.0, longitude: 3.0));
      recorder.addPoint((latitude: 6.001, longitude: 3.001));
      final geojson = recorder.toGeoJson();

      expect(geojson['type'], 'LineString');
      expect(geojson['coordinates'], [
        [3.0, 6.0],
        [3.001, 6.001],
      ]);
    });

    test('single point is not a usable trace', () {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.0, longitude: 3.0));
      expect(recorder.hasUsableTrace, isFalse);
    });
  });

  group('vendor repository', () {
    late AppDatabase db;
    late SyncService sync;
    late ProviderContainer container;
    late FakeHttpAdapter adapter;

    final freshToken = fakeJwt(expiry: DateTime.now().toUtc().add(const Duration(minutes: 15)));

    setUp(() async {
      db = AppDatabase(NativeDatabase.memory());
      adapter = FakeHttpAdapter();
      final store = InMemoryTokenStore();
      await store.save(accessToken: freshToken, refreshToken: 'r', loginMode: LoginMode.vendor);
      final dio = Dio(BaseOptions(baseUrl: 'https://test.local'));
      dio.httpClientAdapter = adapter;
      final client = ApiClient(baseUrl: 'https://test.local', tokenStore: store, dio: dio);
      sync = SyncService(
        db: db,
        api: client,
        connectivity: FakeConnectivity(online: false),
        delay: (_) async {},
      );
      container = ProviderContainer(overrides: [
        apiClientProvider.overrideWithValue(client),
        syncServiceProvider.overrideWithValue(sync),
      ]);
    });

    tearDown(() async {
      container.dispose();
      await sync.dispose();
      await db.close();
    });

    test('detail exposes resubmission pre-fill', () async {
      adapter.on('GET', '/api/v1/field/projects/p-1', (_) => (200, {
            'project': {'id': 'p-1', 'status': 'in_progress'},
            'submissions': [
              {'id': 's-2', 'status': 'rejected', 'actual_length_meters': 120.0, 'review_notes': 'Path too short'},
            ],
            'rejected_for_resubmission': {
              'id': 's-2',
              'status': 'rejected',
              'actual_length_meters': 120.0,
              'review_notes': 'Path too short',
            },
            'attachment_count': 2,
          }));

      final detail = await container.read(vendorRepositoryProvider).fetchDetail('p-1');
      expect(detail.rejectedForResubmission, isNotNull);
      expect(detail.rejectedForResubmission!.actualLengthMeters, 120.0);
      expect(detail.rejectedForResubmission!.reviewNotes, 'Path too short');
    });

    test('fetchProjects parses the {project, lifecycle} list shape', () async {
      adapter.on('GET', '/api/v1/field/projects', (_) => (200, {
            'items': [
              {
                'project': {'id': 'p-1', 'status': 'in_progress', 'notes': 'Gate code 4455'},
                'lifecycle': {
                  'quote': {'status': 'submitted', 'total': 150000.0, 'currency': 'NGN'},
                  'as_built': null,
                  'billing': null,
                },
              },
            ],
            'count': 1,
            'limit': 50,
            'offset': 0,
          }));

      final items = await container.read(vendorRepositoryProvider).fetchProjects();
      expect(items.single.project.id, 'p-1');
      expect(items.single.project.notes, 'Gate code 4455');
      expect(items.single.lifecycle!.quote!.status, 'submitted');
      expect(items.single.lifecycle!.quote!.label, 'NGN 150000');
      expect(items.single.lifecycle!.asBuilt, isNull);
    });

    test('detail parses site contact and lifecycle', () async {
      adapter.on('GET', '/api/v1/field/projects/p-9', (_) => (200, {
            'project': {'id': 'p-9', 'status': 'in_progress'},
            'site': {
              'name': 'Acme Corp',
              'phone': '+2348010000000',
              'address_text': '12 Fiber Way, Lagos',
              'access_notes': 'Ask for the foreman',
            },
            'lifecycle': {
              'quote': {'status': 'approved', 'total': 90000.0, 'currency': 'NGN'},
              'as_built': {'status': 'submitted'},
              'billing': {'status': 'submitted', 'invoice_number': 'PINV-1', 'erp_synced': true},
            },
            'submissions': [],
            'rejected_for_resubmission': null,
            'attachment_count': 0,
          }));

      final detail = await container.read(vendorRepositoryProvider).fetchDetail('p-9');
      expect(detail.site!.name, 'Acme Corp');
      expect(detail.site!.phone, '+2348010000000');
      expect(detail.site!.accessNotes, 'Ask for the foreman');
      expect(detail.site!.hasContact, isTrue);
      expect(detail.lifecycle!.asBuilt!.status, 'submitted');
      expect(detail.lifecycle!.billing!.label, 'Synced to ERP');
    });

    test('submit queues an as_built outbox entry with the right shape', () async {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.0, longitude: 3.0));
      recorder.addPoint((latitude: 6.001, longitude: 3.001));
      recorder.stop();

      await container.read(vendorRepositoryProvider).submitAsBuilt(
            projectId: 'p-1',
            geojson: recorder.toGeoJson(),
            actualLengthMeters: recorder.distanceMeters,
          );

      final rows = await db.select(db.outboxEntries).get();
      expect(rows.single.kind, 'as_built');
      final payload = (jsonDecode(rows.single.payloadJson) as Map).cast<String, dynamic>();
      expect(payload['project_id'], 'p-1');
      expect(payload['geojson']['type'], 'LineString');
      expect(payload['actual_length_meters'], greaterThan(100));
      // No variation / line items were supplied → keys omitted.
      expect(payload.containsKey('variation_type'), isFalse);
      expect(payload.containsKey('line_items'), isFalse);
    });

    test('submit includes variation type and line items when supplied', () async {
      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.0, longitude: 3.0));
      recorder.addPoint((latitude: 6.001, longitude: 3.001));
      recorder.stop();

      await container.read(vendorRepositoryProvider).submitAsBuilt(
            projectId: 'p-1',
            geojson: recorder.toGeoJson(),
            actualLengthMeters: recorder.distanceMeters,
            variationType: 'route_deviation',
            lineItems: const [
              AsBuiltLineItem(description: 'Duct 50m', cableType: 'ADSS', fiberCount: 48, quantity: 50, unitPrice: 120),
            ],
          );

      final rows = await db.select(db.outboxEntries).get();
      final payload = (jsonDecode(rows.single.payloadJson) as Map).cast<String, dynamic>();
      expect(payload['variation_type'], 'route_deviation');
      final items = (payload['line_items'] as List).cast<Map>();
      expect(items.single['description'], 'Duct 50m');
      expect(items.single['cable_type'], 'ADSS');
      expect(items.single['fiber_count'], 48);
      expect(items.single['quantity'], 50);
      expect(items.single['unit_price'], 120);
    });

    test('openQuoteDraft posts to the project quote endpoint', () async {
      adapter.on('POST', '/api/v1/field/projects/p-1/quote', (_) => (200, {
            'id': 'q-1',
            'status': 'draft',
            'total': 0,
            'currency': 'NGN',
          }));

      final quote = await container.read(vendorRepositoryProvider).openQuoteDraft('p-1');
      expect(quote.id, 'q-1');
      expect(quote.status, 'draft');
      expect(quote.isEditable, isTrue);
    });

    test('fetchQuote parses the quote, line items and proposed routes', () async {
      adapter.on('GET', '/api/v1/field/quotes/q-1', (_) => (200, {
            'quote': {'id': 'q-1', 'status': 'draft', 'total': 15000, 'currency': 'NGN'},
            'line_items': [
              {'id': 'li-1', 'description': 'Trenching', 'quantity': 3, 'unit_price': 5000, 'amount': 15000},
            ],
            'proposed_routes': [
              {'id': 'rev-1', 'revision_number': 1, 'status': 'submitted'},
            ],
          }));

      final detail = await container.read(vendorRepositoryProvider).fetchQuote('q-1');
      expect(detail.quote.total, 15000);
      expect(detail.lineItems.single.description, 'Trenching');
      expect(detail.lineItems.single.amount, 15000);
      expect(detail.proposedRoutes.single.revisionNumber, 1);
      expect(detail.proposedRoutes.single.status, 'submitted');
    });

    test('removeQuoteLineItem deletes the line', () async {
      var deleted = false;
      adapter.on('DELETE', '/api/v1/field/quotes/q-1/line-items/li-1', (_) {
        deleted = true;
        return (204, <String, dynamic>{});
      });

      await container.read(vendorRepositoryProvider).removeQuoteLineItem('q-1', 'li-1');
      expect(deleted, isTrue);
    });

    test('addProposedRoute posts geojson and length', () async {
      Map<String, dynamic>? sent;
      adapter.on('POST', '/api/v1/field/quotes/q-1/proposed-route', (options) {
        sent = (options.data as Map).cast<String, dynamic>();
        return (201, {'id': 'rev-1', 'quote_id': 'q-1', 'revision_number': 1, 'status': 'submitted'});
      });

      final recorder = TraceRecorder()..start();
      recorder.addPoint((latitude: 6.0, longitude: 3.0));
      recorder.addPoint((latitude: 6.001, longitude: 3.001));
      recorder.stop();
      await container.read(vendorRepositoryProvider).addProposedRoute('q-1', recorder.toGeoJson(), recorder.distanceMeters);

      expect(sent?['geojson']['type'], 'LineString');
      expect((sent?['length_meters'] as num) > 100, isTrue);
    });

    test('addQuoteLineItem queues an idempotent outbox entry', () async {
      await container.read(vendorRepositoryProvider).addQuoteLineItem(
            'q-1',
            const AsBuiltLineItem(description: 'Trenching', quantity: 3, unitPrice: 5000),
          );

      final rows = await db.select(db.outboxEntries).get();
      expect(rows.single.kind, 'quote_line_item');
      final payload = (jsonDecode(rows.single.payloadJson) as Map).cast<String, dynamic>();
      expect(payload['quote_id'], 'q-1');
      expect(payload['description'], 'Trenching');
      // client_ref is present and matches the outbox row's dedupe key.
      expect(payload['client_ref'], rows.single.clientRef);
    });

    test('submitQuote posts to the submit endpoint', () async {
      adapter.on('POST', '/api/v1/field/quotes/q-1/submit', (_) => (200, {
            'id': 'q-1',
            'status': 'submitted',
            'total': 15000,
            'currency': 'NGN',
          }));

      final quote = await container.read(vendorRepositoryProvider).submitQuote('q-1');
      expect(quote.status, 'submitted');
      expect(quote.isEditable, isFalse);
    });
  });
}
