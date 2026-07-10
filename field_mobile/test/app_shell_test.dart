import 'package:dotmac_field/app/app.dart';
import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/location/map_coordinates.dart';
import 'package:dotmac_field/core/location/location_source.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/features/auth/auth_state.dart';
import 'package:dotmac_field/features/jobs/job_models.dart';
import 'package:dotmac_field/features/jobs/jobs_providers.dart';
import 'package:dotmac_field/features/location/location_cadence.dart';
import 'package:dotmac_field/features/location/location_ping_service.dart';
import 'package:dotmac_field/features/profile/profile_screen.dart';
import 'package:dotmac_field/features/vendor/vendor_map_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _AuthedController extends AuthController {
  @override
  AuthState build() => const Authenticated(LoginMode.staff);
}

class _VendorController extends AuthController {
  @override
  AuthState build() => const Authenticated(LoginMode.vendor);
}

class _UnauthedController extends AuthController {
  @override
  AuthState build() => const Unauthenticated();
}

Widget _app({
  bool authenticated = true,
  LocationPingService? locationPingService,
  AuthController Function() controller = _AuthedController.new,
  List<Override> extra = const [],
}) {
  return ProviderScope(
    overrides: [
      if (locationPingService != null)
        locationPingServiceProvider.overrideWithValue(locationPingService),
      if (!authenticated)
        authControllerProvider.overrideWith(_UnauthedController.new),
      if (authenticated) ...[
        authControllerProvider.overrideWith(controller),
        ...extra,
        meProvider.overrideWith(
          (ref) async => const MeSummary(
            name: 'Chidi Tech',
            openJobs: 2,
            completedToday: 1,
          ),
        ),
        jobsListProvider.overrideWith(
          (ref) async => const JobList(<JobSummary>[]),
        ),
        todayJobsProvider.overrideWith(
          (ref) async => const JobList(<JobSummary>[]),
        ),
        allAssignedJobsProvider.overrideWith(
          (ref) async => const JobList(<JobSummary>[]),
        ),
        // SyncStatusBar reads these; empty streams keep it off-screen without
        // needing a real SyncService.
        pendingOutboxProvider.overrideWith(
          (ref) => Stream.value(<OutboxEntry>[]),
        ),
        conflictOutboxProvider.overrideWith(
          (ref) => Stream.value(<OutboxEntry>[]),
        ),
        pendingPhotosProvider.overrideWith((ref) => Stream.value(0)),
      ],
    ],
    child: const DotmacFieldApp(),
  );
}

void main() {
  testWidgets('unauthenticated users land on the login screen', (tester) async {
    await tester.pumpWidget(_app(authenticated: false));
    await tester.pumpAndSettle();

    expect(find.text('DotMac Field'), findsOneWidget);
    expect(find.text('Sign in'), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
  });

  testWidgets('authenticated shell renders four-tab navigation', (
    tester,
  ) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    expect(find.text('Hello, Chidi'), findsOneWidget);
    expect(find.text('Map'), findsOneWidget);
    expect(find.text('Schedule'), findsOneWidget);
    expect(find.text('Profile'), findsOneWidget);
    expect(find.byType(NavigationBar), findsOneWidget);
  });

  testWidgets('tapping a tab switches branch', (tester) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    await tester.tap(find.text('Schedule'));
    await tester.pumpAndSettle();
    expect(find.text('Schedule'), findsWidgets);
  });

  testWidgets('vendor shell shows Map and Profile only', (tester) async {
    await tester.pumpWidget(
      _app(
        controller: _VendorController.new,
        extra: [
          vendorNearbyPlantProvider.overrideWith(
            (ref) async =>
                const VendorMapData(center: defaultMapCenter, assets: []),
          ),
        ],
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.text('Nearby plant'), findsOneWidget);
    expect(find.text('Map'), findsOneWidget); // vendor-scoped nearby-plant map
    expect(find.text('Profile'), findsOneWidget);
    // The require_technician tabs stay hidden for vendors (would 403).
    expect(find.text('Projects'), findsNothing);
    expect(find.text('Schedule'), findsNothing);
    expect(find.text('Materials'), findsNothing);
    expect(find.text('Customers'), findsNothing);
    expect(find.text('Sales'), findsNothing);
  });

  testWidgets('start shift enables mobile location sharing', (tester) async {
    final calls = <({bool enabled, ShiftState shift})>[];
    final locationService = LocationPingService(
      location: FakeLocation(null),
      poster: (_) async => true,
      sharingUpdater: ({required enabled, required shift}) async {
        calls.add((enabled: enabled, shift: shift));
        return true;
      },
    );

    await tester.pumpWidget(_app(locationPingService: locationService));
    await tester.pumpAndSettle();

    expect(find.text('Location sharing'), findsOneWidget);
    await tester.tap(find.text('Shift'));
    await tester.pumpAndSettle();

    expect(locationService.shift, ShiftState.onShift);
    expect(calls.single.enabled, isTrue);
    expect(calls.single.shift, ShiftState.onShift);
  });
}
