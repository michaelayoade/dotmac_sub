import 'package:dotmac_portal/src/features/service/connection_status_screen.dart';
import 'package:dotmac_portal/src/models/connection_status.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

Widget _app(ConnectionStatus status) => ProviderScope(
      overrides: [
        connectionStatusProvider.overrideWith((ref) => status),
      ],
      child: const MaterialApp(home: ConnectionStatusScreen()),
    );

void main() {
  group('ConnectionStatus.fromJson', () {
    test('parses the full customer-safe payload', () {
      final s = ConnectionStatus.fromJson({
        'state': 'trouble',
        'headline': 'Router not responding',
        'message': "Your router isn't responding.",
        'advice': 'Power it off, wait 30 seconds, then on.',
        'medium': 'fiber',
        'area_outage': false,
        'checked_at': '2026-07-06T11:00:00+00:00',
      });
      expect(s.state, ConnectionHealth.trouble);
      expect(s.headline, 'Router not responding');
      expect(s.advice, isNotNull);
      expect(s.medium, 'fiber');
      expect(s.areaOutage, isFalse);
      expect(s.isConnected, isFalse);
      expect(s.checkedAt, isNotNull);
    });

    test('maps each state string, unknown for anything else', () {
      expect(
          ConnectionHealth.fromWire('connected'), ConnectionHealth.connected);
      expect(ConnectionHealth.fromWire('trouble'), ConnectionHealth.trouble);
      expect(ConnectionHealth.fromWire('outage'), ConnectionHealth.outage);
      expect(ConnectionHealth.fromWire('weird'), ConnectionHealth.unknown);
      expect(ConnectionHealth.fromWire(null), ConnectionHealth.unknown);
    });

    test('tolerates the calm no-service fallback (null advice/medium/checked)',
        () {
      final s = ConnectionStatus.fromJson({
        'state': 'connected',
        'headline': 'No active service',
        'message': 'Nothing to check.',
        'advice': null,
        'medium': null,
        'area_outage': false,
        'checked_at': null,
      });
      expect(s.isConnected, isTrue);
      expect(s.advice, isNull);
      expect(s.medium, isNull);
      expect(s.checkedAt, isNull);
    });
  });

  group('ConnectionStatusScreen', () {
    testWidgets('trouble: shows headline, message and the advice card',
        (tester) async {
      await tester.pumpWidget(_app(const ConnectionStatus(
        state: ConnectionHealth.trouble,
        headline: 'Router not responding',
        message: "Your router isn't responding.",
        advice: 'Reboot your router',
        medium: 'fiber',
        areaOutage: false,
      )));
      await tester.pumpAndSettle();

      expect(find.text('Router not responding'), findsOneWidget);
      expect(find.text("Your router isn't responding."), findsOneWidget);
      expect(find.text('Reboot your router'), findsOneWidget);
    });

    testWidgets(
        'area outage: shows the "we\'re on it" note and SUPPRESSES '
        'any self-blame advice', (tester) async {
      await tester.pumpWidget(_app(const ConnectionStatus(
        state: ConnectionHealth.outage,
        headline: 'Service interruption in your area',
        message: 'A known interruption is affecting your area.',
        // Even if advice leaks through, the UI must not render self-blame
        // during a known area outage.
        advice: 'Reboot your router',
        medium: 'fiber',
        areaOutage: true,
      )));
      await tester.pumpAndSettle();

      expect(find.textContaining('known outage in your area'), findsOneWidget);
      expect(find.text('Reboot your router'), findsNothing);
    });

    testWidgets('connected: renders the healthy headline', (tester) async {
      await tester.pumpWidget(_app(const ConnectionStatus(
        state: ConnectionHealth.connected,
        headline: "You're connected",
        message: 'Your connection looks healthy.',
        areaOutage: false,
      )));
      await tester.pumpAndSettle();

      expect(find.text("You're connected"), findsOneWidget);
    });
  });
}
