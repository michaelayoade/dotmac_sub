import 'package:dotmac_portal/src/features/home/dashboard_screen.dart';
import 'package:dotmac_portal/src/models/connection_status.dart';
import 'package:dotmac_portal/src/models/usage.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

// The Home `ConnectionBanner` must show the OUTAGE-CLASSIFIER verdict when it's
// loaded (so it agrees with the /connection screen and the web portal), and
// fall back to the session-derived rendering when the verdict isn't ready.

Widget _host(Widget child) => MaterialApp(home: Scaffold(body: child));

AccountingSession _session({String? ip, DateTime? start}) => AccountingSession(
      id: '1',
      subscriptionId: 's1',
      sessionId: 'x',
      statusType: 'Start',
      sessionStart: start,
      framedIpAddress: ip,
    );

void main() {
  group('ConnectionBanner — classifier is the source of truth', () {
    testWidgets('connected: healthy "Connected" even with no live session',
        (tester) async {
      await tester.pumpWidget(_host(const ConnectionBanner(
        session: null,
        known: true,
        serviceActive: true,
        classifier: ConnectionStatus(
          state: ConnectionHealth.connected,
          headline: "You're connected",
          message: 'Your connection looks healthy.',
          areaOutage: false,
        ),
      )));

      expect(find.text('Connected'), findsOneWidget);
      // Nothing to troubleshoot when healthy → not a drill-in.
      expect(find.byIcon(Icons.chevron_right), findsNothing);
    });

    testWidgets('connected: still surfaces the live session detail (IP)',
        (tester) async {
      await tester.pumpWidget(_host(ConnectionBanner(
        session: _session(ip: '10.0.0.5'),
        known: true,
        serviceActive: true,
        classifier: const ConnectionStatus(
          state: ConnectionHealth.connected,
          headline: "You're connected",
          message: 'Healthy.',
          areaOutage: false,
        ),
      )));

      expect(find.textContaining('10.0.0.5'), findsOneWidget);
    });

    testWidgets(
        'trouble: amber treatment shows the classifier headline, the '
        'ONE action (advice), and drills in', (tester) async {
      await tester.pumpWidget(_host(const ConnectionBanner(
        session: null,
        known: true,
        serviceActive: true,
        classifier: ConnectionStatus(
          state: ConnectionHealth.trouble,
          headline: 'Router not responding',
          message: "Your router isn't responding.",
          advice: 'Reboot your router',
          areaOutage: false,
        ),
      )));

      expect(find.text('Router not responding'), findsOneWidget);
      expect(find.text('Reboot your router'), findsOneWidget);
      expect(find.byIcon(Icons.chevron_right), findsOneWidget);
    });

    testWidgets(
        'outage + area outage: red-state headline with the reassuring message '
        'and NO self-blame advice', (tester) async {
      await tester.pumpWidget(_host(const ConnectionBanner(
        session: null,
        known: true,
        serviceActive: true,
        classifier: ConnectionStatus(
          state: ConnectionHealth.outage,
          headline: 'Service interruption in your area',
          message: "A known interruption is affecting your area — we're on it.",
          // Even if advice leaks through, the banner must not self-blame under
          // a known area outage.
          advice: 'Reboot your router',
          areaOutage: true,
        ),
      )));

      expect(find.text('Service interruption in your area'), findsOneWidget);
      expect(find.textContaining("we're on it"), findsOneWidget);
      expect(find.text('Reboot your router'), findsNothing);
      // A problem to explain → drills into the troubleshooter.
      expect(find.byIcon(Icons.chevron_right), findsOneWidget);
    });

    testWidgets(
        'FALLBACK: classifier null → session-derived "Connected · <ip>"',
        (tester) async {
      await tester.pumpWidget(_host(ConnectionBanner(
        session: _session(ip: '10.0.0.9'),
        known: true,
        serviceActive: true,
        classifier: null,
      )));

      expect(find.textContaining('Connected'), findsOneWidget);
      expect(find.textContaining('10.0.0.9'), findsOneWidget);
    });

    testWidgets(
        'FALLBACK: classifier null + no session → session-derived '
        '"Not connected"', (tester) async {
      await tester.pumpWidget(_host(const ConnectionBanner(
        session: null,
        known: true,
        serviceActive: true,
        classifier: null,
      )));

      expect(find.textContaining('Not connected'), findsOneWidget);
      expect(find.byIcon(Icons.chevron_right), findsOneWidget);
    });
  });
}
