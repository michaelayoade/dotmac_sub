import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../auth/biometric_enrollment_prompt.dart';
import '../service/connection_status_screen.dart' show connectionVisual;
import '../../models/project.dart';
import '../../models/connection_status.dart';
import '../../models/service_status.dart';
import '../../models/subscription.dart';
import '../../models/usage.dart';
import '../../models/work_order.dart';
import '../../providers/auth_controller.dart';
import '../../providers/data_providers.dart';
import '../../providers/read_notifications.dart';
import '../../widgets/account_avatar_button.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/offline_banner.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';

/// Home dashboard: an at-a-glance summary (account status, balance, data,
/// services) plus quick-action shortcuts into the rest of the app.
/// Short, human ETA for the Home visit banner. Prefers the live estimate, then
/// falls back to a plain "on the way" (the tracking screen has the detail).
String _visitEta(WorkOrderItem w) {
  final eta = w.estimatedArrivalAt;
  if (eta != null) {
    final mins = eta.difference(DateTime.now()).inMinutes;
    if (mins > 1) return 'arriving in ~$mins min';
    if (mins >= -15) return 'arriving now';
  }
  return 'on the way';
}

class DashboardScreen extends ConsumerWidget {
  const DashboardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final me = ref.watch(currentUserProvider);
    final subs = ref.watch(subscriptionsProvider);
    final serviceStatus = ref.watch(serviceStatusProvider).asData?.value;
    final invoices = ref.watch(invoicesProvider);
    final sessions = ref.watch(accountingSessionsProvider);
    final notifications = ref.watch(notificationsProvider);
    ref.watch(notificationReadMigrationProvider);

    final unread =
        notifications.asData?.value.items.where((n) => !n.isRead).length ?? 0;

    // --- Summary values (null while loading) ---
    final subList = subs.asData?.value.items;

    // The service the dashboard is "about": the user's switcher pick, else
    // the shared current-service rule. Drives the days-left stat and the
    // Current service card below.
    Subscription? currentService;
    if (subList != null && subList.isNotEmpty) {
      final selectedId = ref.watch(selectedServiceIdProvider);
      currentService = subList.firstWhere(
        (s) => s.id == selectedId,
        orElse: () => pickCurrentService(subList),
      );
    }
    final currentServiceStatus = currentService == null
        ? null
        : serviceStatus?.forSubscription(currentService.id);
    final unavailableServices =
        serviceStatus?.unavailableServices ?? const <ServiceStatusItem>[];
    final statusAction =
        unavailableServices.isNotEmpty ? serviceStatus?.primaryAction : null;

    final invItems = invoices.asData?.value.items;
    final outstanding = invItems
        ?.where((i) => !i.isPaid)
        .fold<double>(0, (sum, i) => sum + i.balanceDue);
    final currency = (invItems != null && invItems.isNotEmpty)
        ? invItems.first.currency
        : 'NGN';

    final sessItems = sessions.asData?.value.items;
    // Defined-window total (today) instead of summing the latest 50 sessions.
    final todaySummary = ref.watch(usageSummaryProvider('today')).asData?.value;
    final fup = todaySummary?.fup;
    // Two separate at-a-glance usage figures: today, and the whole billing/
    // subscription period (cycle) — the latter meaningful for capped AND
    // unlimited plans, unlike "data left" which reads as 0 on unlimited.
    final cycleSummary = ref.watch(usageSummaryProvider('cycle')).asData?.value;
    final dataToday = todaySummary?.totalBytes;
    // The cycle headline is a server-owned RADIUS-session total. An
    // authoritative zero means no recorded usage in the window; it is not a
    // missing-data sentinel and must never be replaced with the loaded-session
    // page, retention-limited chart series, or today's different window.
    final dataPeriod = cycleSummary?.authoritativeTotalBytes;
    // Wallet (account credit) balance for its own at-a-glance card. Uses the
    // always-available credit balance (/me/balance), not the feature-gated VAS
    // wallet (/me/wallet 404s when vas.enabled is off → card never reads).
    final balance = ref.watch(balanceProvider).asData?.value;
    // Peak throughput for the "Peak" tile — shown per direction (↓ download,
    // ↑ upload), subscriber perspective. Prefer the exact billing-cycle peak
    // from the cycle summary; fall back to the ~30d stats window.
    final peak30 = ref.watch(peakBandwidthProvider).asData?.value;
    final peakDownBps =
        cycleSummary?.peakDownloadBps ?? peak30?.peakDownloadBps;
    final peakUpBps = cycleSummary?.peakUploadBps ?? peak30?.peakUploadBps;
    final peakLoaded = cycleSummary != null || peak30 != null;
    String mbps(double? b) => (b == null || b <= 0)
        ? '—'
        : (b / 1e6).toStringAsFixed(b >= 1e7 ? 0 : 1);
    final peakHasData = (peakDownBps != null && peakDownBps > 0) ||
        (peakUpBps != null && peakUpBps > 0);
    final peakValue = !peakLoaded
        ? null // still loading
        : (!peakHasData
            ? '—'
            : '↑${mbps(peakUpBps)} ↓${mbps(peakDownBps)} Mbps');

    // Current period's quota bucket for the current service, when the plan is
    // capped — drives the usage bar on the service card.
    final quotaBuckets = ref.watch(quotaBucketsProvider).asData?.value;
    QuotaBucket? currentQuota;
    if (quotaBuckets != null && currentService != null) {
      final now = DateTime.now();
      for (final b in quotaBuckets) {
        if (b.subscriptionId != currentService.id || b.isUnlimited) continue;
        if (b.periodEnd.isBefore(now)) continue;
        if (currentQuota == null ||
            b.periodStart.isAfter(currentQuota.periodStart)) {
          currentQuota = b;
        }
      }
    }

    // Quota / fair-use headroom, for plans where it applies. Capped plans show
    // remaining allowance; unlimited-with-FUP plans show GB left at full speed.
    // Null (card hidden) for truly unlimited plans with no fair-use policy.
    String? quotaLeftValue;
    var quotaLeftLabel = 'Data left';
    if (currentQuota != null && currentQuota.remainingGb != null) {
      quotaLeftValue = Fmt.gb(currentQuota.remainingGb!);
    } else if (fup?.gbUntilThrottle != null && (fup?.thresholdGb ?? 0) > 0) {
      quotaLeftValue = Fmt.gb(fup!.gbUntilThrottle!);
      quotaLeftLabel = 'Full-speed';
    }

    // Expiry urgency: lift a renew prompt to the banner area when the current
    // service is within 3 days of lapsing (the payment banner takes priority).
    // An *active* service is never "expired" — a momentarily-stale billing date
    // must not nag a running service; genuine lapses surface via [isExpired]
    // (a non-active current service). Postpaid has no date expiry at all.
    final daysLeft = currentService?.daysUntilExpiry;
    // Third stat card: expiry countdown for date-expiry plans, else next-bill.
    final (expiryStatLabel, expiryStatValue) =
        _expiryOrBillingStat(subList, currentService);
    String? renewMessage;
    ServiceStatusAction? renewAction;
    if (currentService != null && currentService.isActive) {
      final name = currentService.displayName;
      if (currentService.isExpired) {
        renewMessage = '$name has expired — renew now';
      } else if (currentServiceStatus?.action?.isFinancial ?? false) {
        // The real, balance/dunning-driven nudge: a running service heading for
        // a cut the customer can prevent by paying. The cut date (if known)
        // comes from the prepaid grace timer — never from a billing date.
        renewAction = currentServiceStatus!.action;
        renewMessage = renewAction!.message;
      } else if (daysLeft != null && daysLeft >= 0 && daysLeft <= 3) {
        // Contract end approaching (the only genuine date-based expiry).
        renewMessage = switch (daysLeft) {
          0 => '$name expires today — renew now',
          1 => '$name expires tomorrow — renew now',
          final d => '$name expires in $d days — renew now',
        };
      }
    }

    // Connection status: an open RADIUS accounting session (no end) means the
    // subscriber is currently online; its start gives the uptime.
    AccountingSession? activeSession;
    if (sessItems != null) {
      for (final s in sessItems) {
        if (s.isActive) {
          activeSession = s;
          break;
        }
      }
    }

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            // Brand wordmark (white-label drop-in asset); falls back to just
            // the greeting if a deployment ships without one.
            Image.asset(
              'assets/images/login_logo.png',
              height: 22,
              errorBuilder: (_, __, ___) => const SizedBox.shrink(),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                'Hi, ${me?.firstName ?? 'there'}',
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Notifications',
            onPressed: () => context.go('/dashboard/notifications'),
            icon: Badge(
              isLabelVisible: unread > 0,
              label: Text('$unread'),
              child: const Icon(Icons.notifications_outlined),
            ),
          ),
          const AccountAvatarButton(),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(subscriptionsProvider);
          ref.invalidate(serviceStatusProvider);
          ref.invalidate(invoicesProvider);
          ref.invalidate(accountingSessionsProvider);
          ref.invalidate(usageSummaryProvider('today'));
          ref.invalidate(quotaBucketsProvider);
          ref.invalidate(liveBandwidthProvider);
          ref.invalidate(peakBandwidthProvider);
          await Future.wait([
            ref.read(subscriptionsProvider.future),
            ref.read(invoicesProvider.future),
          ]);
        },
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            const BiometricEnrollmentPrompt(),
            const OfflineBanner(),
            ConnectionBanner(
              session: activeSession,
              known: sessions.hasValue,
              serviceActive: currentService?.isActive ?? false,
              ipAddress: currentService?.ipv4Address,
              // Outage-classifier verdict (P4): lets the banner suppress
              // "check your router" during a known area outage and drill into
              // the connection troubleshooter.
              classifier: ref.watch(connectionStatusProvider).asData?.value,
            ),
            const SizedBox(height: 12),
            _StatusBanner(
              attentionMessage: unavailableServices.isNotEmpty
                  ? statusAction?.message ??
                      'A service is unavailable — contact support for help.'
                  : null,
              known: serviceStatus != null,
              onTap: statusAction == null
                  ? null
                  : () => _openServiceAction(context, statusAction),
            ),
            if (fup != null && (fup.needsAttention || fup.isApproaching)) ...[
              const SizedBox(height: 12),
              _FupBanner(fup: fup, onTap: () => context.go('/usage')),
            ],
            if (renewMessage != null) ...[
              const SizedBox(height: 12),
              _RenewBanner(
                message: renewMessage,
                expired: currentService?.isExpired ?? false,
                onTap: renewAction == null
                    ? () => context.go('/billing')
                    : () => _openServiceAction(context, renewAction!),
              ),
            ],
            // Live technician visit — a slim banner shown only while a work
            // order is in progress. The full map lives on its own screen
            // (/track/:id) so it doesn't crowd the dashboard.
            Consumer(builder: (context, ref, _) {
              final orders =
                  ref.watch(workOrdersProvider).asData?.value.workOrders ??
                      const <WorkOrderItem>[];
              WorkOrderItem? active;
              for (final w in orders) {
                if (w.status == 'in_progress') {
                  active = w;
                  break;
                }
              }
              if (active == null) return const SizedBox.shrink();
              final v = active;
              final scheme = Theme.of(context).colorScheme;
              final who = v.technicianName ?? 'Your technician';
              return Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Card(
                  shape: RoundedRectangleBorder(
                    side: BorderSide(
                      color: scheme.primary.withValues(alpha: 0.45),
                    ),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: ListTile(
                    leading: CircleAvatar(
                      backgroundColor: scheme.primaryContainer,
                      foregroundColor: scheme.onPrimaryContainer,
                      child: const Icon(Icons.engineering_outlined),
                    ),
                    title: const Text(
                      'Technician on the way',
                      style: TextStyle(fontWeight: FontWeight.w600),
                    ),
                    subtitle: Text('$who · ${_visitEta(v)}'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/track/${v.id}'),
                  ),
                ),
              );
            }),
            // Installation progress — a modest, secondary banner shown only
            // while an install is under way. Onboarding is a one-time activity,
            // so it's deliberately low-key (muted, no accent) vs. the visit
            // banner; it links to the tracker and disappears once complete.
            Consumer(builder: (context, ref, _) {
              final projects =
                  ref.watch(projectsProvider).asData?.value.projects ??
                      const <ProjectItem>[];
              ProjectItem? install;
              for (final p in projects) {
                if (p.progressPct < 100) {
                  install = p;
                  break;
                }
              }
              if (install == null) return const SizedBox.shrink();
              final p = install;
              final scheme = Theme.of(context).colorScheme;
              final stage = (p.currentStage?.isNotEmpty ?? false)
                  ? p.currentStage!
                  : 'Setting up your service';
              return Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Card(
                  color: scheme.surfaceContainerHighest,
                  elevation: 0,
                  child: ListTile(
                    leading: Icon(
                      Icons.timeline_outlined,
                      color: scheme.onSurfaceVariant,
                    ),
                    title: const Text('Installation in progress'),
                    subtitle: Text('$stage · ${p.progressPct}%'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/profile/installation-progress'),
                  ),
                ),
              );
            }),
            Consumer(builder: (context, ref, _) {
              final wallet = ref.watch(walletProvider).asData?.value;
              if (wallet == null) return const SizedBox.shrink();
              return Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Card(
                  child: ListTile(
                    leading: const Icon(Icons.wallet_outlined),
                    title: Text(Fmt.money(wallet.balance, wallet.currency)),
                    subtitle: const Text('Wallet — fund once, pay bills'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => context.push('/wallet'),
                  ),
                ),
              );
            }),
            const SizedBox(height: 16),

            // --- At-a-glance summary (rows of 3; grid pads the last row) ---
            _StatGrid(
              tiles: [
                _StatCard(
                  icon: Icons.account_balance_wallet_outlined,
                  label: 'Wallet',
                  value: balance == null
                      ? null
                      : Fmt.moneyCompact(
                          balance.creditBalance, balance.currency),
                  onTap: () => context.push('/wallet'),
                ),
                _StatCard(
                  icon: Icons.receipt_long_outlined,
                  label: 'Amount due',
                  value: outstanding == null
                      ? null
                      : Fmt.moneyCompact(outstanding, currency),
                  // Label already says "Amount due"; highlight when > 0.
                  highlight: (outstanding ?? 0) > 0,
                  onTap: () => context.go('/billing'),
                ),
                _StatCard(
                  icon: Icons.today_outlined,
                  label: 'Today',
                  value: dataToday == null ? null : Fmt.bytes(dataToday),
                  onTap: () => context.go('/usage'),
                ),
                _StatCard(
                  icon: Icons.data_usage_outlined,
                  // Total data used this billing/subscription period.
                  label: 'This period',
                  value: cycleSummary == null
                      ? null
                      : dataPeriod == null
                          ? '—'
                          : Fmt.bytes(dataPeriod),
                  highlight: (fup?.isApproaching ?? false) ||
                      (fup?.needsAttention ?? false),
                  onTap: () => context.go('/usage'),
                ),
                // Quota / fair-use remaining — only for plans where it applies.
                if (quotaLeftValue != null)
                  _StatCard(
                    icon: Icons.data_saver_off_outlined,
                    label: quotaLeftLabel,
                    value: quotaLeftValue,
                    highlight: (currentQuota != null &&
                            (currentQuota.usedFraction ?? 0) >= 0.9) ||
                        (fup?.isApproaching ?? false) ||
                        (fup?.needsAttention ?? false),
                    onTap: () => context.go('/usage'),
                  ),
                _StatCard(
                  icon: Icons.speed_outlined,
                  // Peak download throughput over the billing period (cycle
                  // peak; ~30d stats as fallback). Labelled so customers know
                  // the window — matches the "This period" usage tile.
                  label: 'Peak this period',
                  value: peakValue,
                  onTap: () => context.go('/usage'),
                ),
                _StatCard(
                  icon: Icons.event_outlined,
                  label: expiryStatLabel,
                  value: expiryStatValue,
                  // Urgent when expiring within 3 days or genuinely expired.
                  highlight: (currentService?.expiresSoon ?? false) ||
                      (currentService?.isExpired ?? false),
                  onTap: () => context.go('/billing'),
                ),
              ],
            ),
            const SizedBox(height: 20),

            // --- Primary payment action ---
            // A single, prominent "Add funds / Pay" entry (the wallet top-up
            // flow), replacing the redundant "Pay bill" + "Top up" chips.
            _AddFundsCard(
              onTap: () => context.push('/topup'),
            ),
            const SizedBox(height: 20),

            // --- Current service (with a switcher when there are several) ---
            const _SectionHeader('Current service'),
            Builder(
              builder: (context) {
                // Stale-while-revalidate: keep showing the last-known
                // service(s) — with a quiet "couldn't refresh" banner — instead
                // of replacing the card with an error when a refresh fails under
                // load. The on-disk cache means `subs` usually still has a value
                // even on a cold-start network blip.
                if (subs.hasValue) {
                  final services = subs.requireValue.items;
                  if (services.isEmpty) {
                    return const _MessageCard('No active service found.');
                  }
                  final selectedId = ref.watch(selectedServiceIdProvider);
                  final selected = services.firstWhere(
                    (s) => s.id == selectedId,
                    orElse: () => pickCurrentService(services),
                  );
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      if (subs.hasError && !subs.isLoading)
                        StaleBanner(
                          onRetry: () => ref.invalidate(subscriptionsProvider),
                        ),
                      if (services.length > 1) ...[
                        _ServiceSwitcher(
                          services: services,
                          selectedId: selected.id,
                          onSelect: (id) => ref
                              .read(selectedServiceIdProvider.notifier)
                              .state = id,
                        ),
                        const SizedBox(height: 10),
                      ],
                      _CurrentServiceCard(
                        service: selected,
                        quota: currentQuota,
                        action:
                            serviceStatus?.forSubscription(selected.id)?.action,
                      ),
                    ],
                  );
                }
                return subs.isLoading
                    ? const CardSkeleton()
                    : const _MessageCard(
                        'Couldn’t load your service. Pull down to refresh.');
              },
            ),
          ],
        ),
      ),
    );
  }
}

/// Days-left figure for the stat row: null while loading (renders a
/// skeleton), '—' when the service has no known expiry, otherwise the
/// (urgency-worded) day count.
/// Third stat card: a genuine expiry countdown for date-expiry plans, else the
/// next-bill date for postpaid/unlimited (which has no expiry) so the card is
/// meaningful instead of a bare "—". Returns (label, value).
(String, String?) _expiryOrBillingStat(
  List<Subscription>? subList,
  Subscription? service,
) {
  if (subList == null) return ('Days left', null);
  if (service == null) return ('Days left', '—');
  if (service.isExpired) return ('Days left', 'Expired');
  final days = service.daysUntilExpiry;
  if (days != null && days >= 0) {
    return ('Days left', days == 0 ? 'Today' : '$days');
  }
  // No date-based expiry (postpaid/unlimited): show the next bill instead of a
  // confusing empty "Days left".
  if (service.nextBillingAt != null) {
    return ('Next bill', Fmt.date(service.nextBillingAt));
  }
  return ('Days left', '—');
}

void _openServiceAction(BuildContext context, ServiceStatusAction action) {
  final route = switch (action.kind) {
    'top_up' => '/topup',
    'pay_invoices' => '/billing',
    'view_usage' => '/usage',
    _ => '/support',
  };
  if (action.kind == 'top_up') {
    context.push(route);
  } else {
    context.go(route);
  }
}

/// Network connection status — the headline reason customers open the app.
/// When the outage-classifier verdict ([classifier]) is loaded it is the
/// SOURCE OF TRUTH, so this banner agrees with the /connection screen and the
/// web portal (no more "Connected" here while the screen says "trouble"). It
/// falls back to the live RADIUS-session signal when the verdict isn't ready.
@visibleForTesting
class ConnectionBanner extends StatelessWidget {
  const ConnectionBanner({
    super.key,
    required this.session,
    required this.known,
    this.serviceActive = false,
    this.ipAddress,
    this.classifier,
  });

  /// The outage-classifier verdict for this customer, when loaded. This is the
  /// SOURCE OF TRUTH for the displayed state: connected → healthy, trouble →
  /// amber, outage → red (carrying the classifier's headline/message/advice).
  /// Null while it loads or errors, in which case the banner falls back to the
  /// session-derived rendering below so it never shows a blank/spinner.
  final ConnectionStatus? classifier;

  /// Whether the displayed subscription is active. An active account that is
  /// merely not connected right now (router off, brief drop) is routine — it
  /// gets neutral styling, not the alarming red reserved for real problems.
  final bool serviceActive;

  /// The active session, or null when offline. Only meaningful when [known].
  final AccountingSession? session;

  /// True once the sessions request has resolved with data.
  final bool known;

  /// Fallback IP when the live session carries no framed address
  /// (statically-assigned plans).
  final String? ipAddress;

  /// The friendly "Connected · up 3h · 10.0.0.5" line, built from the live
  /// session when we have one (framed IP is the live dynamic address; the
  /// subscription's assigned IP is the static fallback). Degrades to a bare
  /// "Connected" when the classifier says healthy but no session is loaded.
  String _connectedLine() {
    final s = session;
    final start = s?.sessionStart;
    final ip = s?.framedIpAddress ?? ipAddress;
    return [
      'Connected',
      if (start != null) 'up ${Fmt.uptime(start)}',
      if (ip != null && ip.isNotEmpty) ip,
    ].join(' · ');
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final Color bg;
    final Color fg;
    final IconData icon;
    final String text;
    // Optional second line — the ONE action (classifier `advice`) or the
    // plain-language `message`, shown under the headline for a problem state.
    String? subtitle;
    // Whether tapping drills into the full /connection troubleshooter; there's
    // only something to troubleshoot when we're not cleanly connected.
    final bool tappable;

    final c = classifier;
    if (c != null && c.state == ConnectionHealth.connected) {
      // CLASSIFIER (source of truth): healthy. Keep the friendly filled
      // treatment and enrich the line with the live session detail when we
      // have it (uptime / IP).
      bg = scheme.secondaryContainer;
      fg = scheme.onSecondaryContainer;
      icon = Icons.wifi;
      text = _connectedLine();
      tappable = false;
    } else if (c != null &&
        (c.state == ConnectionHealth.trouble ||
            c.state == ConnectionHealth.outage)) {
      // CLASSIFIER (source of truth): trouble = amber, outage = red — the same
      // visual language as the /connection screen (via [connectionVisual]) so
      // the Home banner can never disagree with it. Under a known area outage
      // the state is `outage` and the classifier's own message is the
      // reassuring "we're on it", so the calm wording falls out naturally here.
      final visual = connectionVisual(context, c.state);
      bg = visual.color.withValues(alpha: 0.12);
      fg = visual.color;
      icon = visual.icon;
      text = c.headline;
      // `advice` is the ONE action; the server nulls it under an area outage
      // (this guard is belt-and-suspenders) so the banner never self-blames.
      subtitle = (c.advice != null && !c.areaOutage) ? c.advice! : c.message;
      tappable = true;
    } else if (!known) {
      // FALLBACK (classifier absent / loading / unknown): the original
      // session-derived rendering, so the banner always shows something sane.
      bg = scheme.surfaceContainerHighest;
      fg = scheme.onSurfaceVariant;
      icon = Icons.wifi_find_outlined;
      text = 'Checking connection…';
      tappable = false;
    } else if (session != null) {
      bg = scheme.secondaryContainer;
      fg = scheme.onSecondaryContainer;
      icon = Icons.wifi;
      text = _connectedLine();
      tappable = false;
    } else if (c?.areaOutage ?? false) {
      // A known area outage above this customer — reassure, don't blame their
      // router. Calm tertiary styling, not alarming red.
      bg = scheme.tertiaryContainer;
      fg = scheme.onTertiaryContainer;
      icon = Icons.cloud_off;
      text = "Known outage in your area — we're on it";
      tappable = true;
    } else if (serviceActive) {
      bg = scheme.surfaceContainerHighest;
      fg = scheme.onSurfaceVariant;
      icon = Icons.wifi_off_outlined;
      text = 'Not connected — tap to troubleshoot';
      tappable = true;
    } else {
      bg = scheme.errorContainer;
      fg = scheme.onErrorContainer;
      icon = Icons.wifi_off_outlined;
      text = 'Offline';
      tappable = true;
    }

    final Widget label = subtitle == null
        ? Text(text, style: TextStyle(color: fg, fontWeight: FontWeight.w600))
        : Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(text,
                  style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
              const SizedBox(height: 2),
              Text(
                subtitle,
                style: TextStyle(
                  color: fg.withValues(alpha: 0.9),
                  fontSize: 12.5,
                ),
              ),
            ],
          );
    final row = Row(
      children: [
        Icon(icon, color: fg),
        const SizedBox(width: 10),
        Expanded(child: label),
        if (tappable) Icon(Icons.chevron_right, color: fg),
      ],
    );
    const shape = BorderRadius.all(Radius.circular(14));
    if (!tappable) {
      return Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(color: bg, borderRadius: shape),
        child: row,
      );
    }
    return Material(
      color: bg,
      borderRadius: shape,
      child: InkWell(
        borderRadius: shape,
        onTap: () => context.push('/connection'),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: row,
        ),
      ),
    );
  }
}

class _StatusBanner extends StatelessWidget {
  const _StatusBanner({
    required this.attentionMessage,
    required this.known,
    this.onTap,
  });

  /// Server-owned action message; null when every service is in good standing.
  final String? attentionMessage;
  final bool known;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final (bg, fg, icon, text) = !known
        ? (
            scheme.surfaceContainerHighest,
            scheme.onSurface,
            Icons.hourglass_empty,
            'Loading your account…'
          )
        : attentionMessage != null
            ? (
                scheme.errorContainer,
                scheme.onErrorContainer,
                Icons.warning_amber_rounded,
                attentionMessage!
              )
            : (
                scheme.primaryContainer,
                scheme.onPrimaryContainer,
                Icons.check_circle_outline,
                'All services active'
              );
    final radius = BorderRadius.circular(14);
    return Material(
      color: bg,
      borderRadius: radius,
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              Icon(icon, color: fg),
              const SizedBox(width: 10),
              Expanded(
                child: Text(text,
                    style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
              ),
              if (onTap != null) Icon(Icons.chevron_right, color: fg),
            ],
          ),
        ),
      ),
    );
  }
}

/// Fair-Usage alert on the dashboard. Taps through to the Usage tab, where the
/// full explainer and "Top up to restore" CTA live.
/// Expiry-urgency prompt lifted to the banner area when the current service
/// is within 3 days of lapsing (or already lapsed).
class _RenewBanner extends StatelessWidget {
  const _RenewBanner({
    required this.message,
    required this.expired,
    this.onTap,
  });
  final String message;
  final bool expired;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final bg = expired ? scheme.errorContainer : scheme.tertiaryContainer;
    final fg = expired ? scheme.onErrorContainer : scheme.onTertiaryContainer;
    return Material(
      color: bg,
      borderRadius: BorderRadius.circular(14),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              Icon(expired ? Icons.error_outline : Icons.schedule, color: fg),
              const SizedBox(width: 10),
              Expanded(
                child: Text(message,
                    style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
              ),
              if (onTap != null) Icon(Icons.chevron_right, color: fg),
            ],
          ),
        ),
      ),
    );
  }
}

class _FupBanner extends StatelessWidget {
  const _FupBanner({required this.fup, this.onTap});
  final FupStatus fup;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final blocked = fup.isBlocked;
    final approaching = fup.isApproaching;
    final bg = blocked
        ? scheme.errorContainer
        : approaching
            ? scheme.secondaryContainer
            : scheme.tertiaryContainer;
    final fg = blocked
        ? scheme.onErrorContainer
        : approaching
            ? scheme.onSecondaryContainer
            : scheme.onTertiaryContainer;
    final text = fup.summary ??
        (blocked
            ? 'Service paused — fair-usage limit reached'
            : approaching
                ? 'Approaching your fair-usage limit'
                : 'Speed reduced — fair-usage limit reached');
    return Material(
      color: bg,
      borderRadius: BorderRadius.circular(14),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              Icon(
                blocked
                    ? Icons.block
                    : approaching
                        ? Icons.data_usage
                        : Icons.speed,
                color: fg,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(text,
                    style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
              ),
              if (onTap != null) Icon(Icons.chevron_right, color: fg),
            ],
          ),
        ),
      ),
    );
  }
}

/// Horizontal chip selector for picking which subscription the "Current
/// service" card shows, when the customer has more than one.
class _ServiceSwitcher extends StatelessWidget {
  const _ServiceSwitcher({
    required this.services,
    required this.selectedId,
    required this.onSelect,
  });
  final List<Subscription> services;
  final String selectedId;
  final ValueChanged<String> onSelect;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 40,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: services.length,
        separatorBuilder: (_, __) => const SizedBox(width: 8),
        itemBuilder: (_, i) {
          final s = services[i];
          return ChoiceChip(
            label: Text(s.displayName,
                maxLines: 1, overflow: TextOverflow.ellipsis),
            selected: s.id == selectedId,
            avatar: Icon(
              s.isActive ? Icons.circle : Icons.pause_circle_outline,
              size: 14,
              color: s.isActive ? context.semantic.success : null,
            ),
            onSelected: (_) => onSelect(s.id),
          );
        },
      ),
    );
  }
}

/// Lays out at-a-glance stat tiles in rows of three, padding the final row so
/// every tile keeps an equal width regardless of count (5, 6 or 7 tiles).
class _StatGrid extends StatelessWidget {
  const _StatGrid({required this.tiles});
  final List<Widget> tiles;

  @override
  Widget build(BuildContext context) {
    const perRow = 3;
    const gap = 10.0;
    final rows = <Widget>[];
    for (var i = 0; i < tiles.length; i += perRow) {
      final cells = <Widget>[];
      for (var j = 0; j < perRow; j++) {
        if (j > 0) cells.add(const SizedBox(width: gap));
        final idx = i + j;
        cells.add(Expanded(
          child: idx < tiles.length ? tiles[idx] : const SizedBox(),
        ));
      }
      if (rows.isNotEmpty) rows.add(const SizedBox(height: gap));
      rows.add(IntrinsicHeight(
          child: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: cells,
      )));
    }
    return Column(children: rows);
  }
}

class _StatCard extends StatelessWidget {
  const _StatCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.onTap,
    this.highlight = false,
  });
  final IconData icon;
  final String label;

  /// Null while the backing request is loading — renders a shimmer skeleton.
  final String? value;
  final VoidCallback onTap;
  final bool highlight;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: EdgeInsets.zero,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(icon, size: 20, color: theme.colorScheme.primary),
              const SizedBox(height: 10),
              // Scale the figure down to fit the narrow column rather than
              // truncating it (a cut-off "NGN 1,732,…" is unreadable).
              if (value == null)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 3),
                  child: Shimmer(child: SkeletonBox(width: 56, height: 16)),
                )
              else
                FittedBox(
                  fit: BoxFit.scaleDown,
                  alignment: Alignment.centerLeft,
                  child: Text(value!,
                      maxLines: 1,
                      softWrap: false,
                      style: theme.textTheme.titleMedium?.copyWith(
                        fontWeight: FontWeight.w700,
                        color: highlight ? theme.colorScheme.error : null,
                      )),
                ),
              const SizedBox(height: 2),
              Text(label,
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.outline)),
            ],
          ),
        ),
      ),
    );
  }
}

/// Primary, visually-dominant payment action on the dashboard. Funds the
/// wallet (which pays bills), folding the old "Pay bill" + "Top up" chips into
/// one clear CTA.
class _AddFundsCard extends StatelessWidget {
  const _AddFundsCard({required this.onTap});
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.primary,
      borderRadius: BorderRadius.circular(14),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
          child: Row(
            children: [
              Icon(Icons.add_card_outlined, color: scheme.onPrimary),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Add funds / Pay',
                      style: TextStyle(
                        color: scheme.onPrimary,
                        fontWeight: FontWeight.w700,
                        fontSize: 16,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      'Top up your wallet to pay bills',
                      style: TextStyle(
                        color: scheme.onPrimary.withValues(alpha: 0.85),
                      ),
                    ),
                  ],
                ),
              ),
              Icon(Icons.chevron_right, color: scheme.onPrimary),
            ],
          ),
        ),
      ),
    );
  }
}

class _CurrentServiceCard extends StatelessWidget {
  const _CurrentServiceCard({required this.service, this.quota, this.action});
  final Subscription service;
  final ServiceStatusAction? action;

  /// Current period's quota bucket, when the plan is capped — renders a thin
  /// usage bar so an approaching cap is visible without opening Usage.
  final QuotaBucket? quota;

  @override
  Widget build(BuildContext context) {
    final s = service;
    final theme = Theme.of(context);
    final days = s.daysUntilExpiry;
    // The validity stat sits next to the IP address; it must never show a red
    // "Expired" for a running (active) service, or it reads as "the IP expired".
    final (expiryColor, expiryLabel, expiryText) = s.isExpired
        ? (theme.colorScheme.error, 'Validity', 'Expired')
        : switch (days) {
            // Postpaid / no date expiry: show the next bill date, not a
            // (meaningless) validity countdown.
            null => s.nextBillingAt != null
                ? (
                    theme.colorScheme.outline,
                    'Next bill',
                    Fmt.date(s.nextBillingAt)
                  )
                : (theme.colorScheme.outline, null, null),
            0 => (theme.colorScheme.error, 'Validity', 'Expires today'),
            // Active service with a momentarily-stale billing date: running, not
            // expired — show nothing rather than alarm next to the IP.
            < 0 => (theme.colorScheme.outline, null, null),
            <= 3 => (
                context.semantic.warning,
                'Validity',
                '$days day${days == 1 ? '' : 's'} left'
              ),
            _ => (context.semantic.success, 'Validity', '$days days left'),
          };

    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => context.push('/service/${s.id}', extra: s),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  const Icon(Icons.router_outlined),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(s.displayName,
                        style: theme.textTheme.titleMedium,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis),
                  ),
                  StatusChip.forSubscription(s.status),
                ],
              ),
              if (s.planType != null) ...[
                const SizedBox(height: 2),
                Text(s.planType!,
                    style: theme.textTheme.bodySmall
                        ?.copyWith(color: theme.colorScheme.outline)),
              ],
              const Divider(height: 20),
              Row(
                children: [
                  Expanded(
                    child: _MiniStat(
                      icon: Icons.lan_outlined,
                      label: 'IP address',
                      value: s.ipv4Address ?? '—',
                    ),
                  ),
                  if (expiryText != null)
                    Expanded(
                      child: _MiniStat(
                        icon: Icons.schedule,
                        label: expiryLabel ?? 'Validity',
                        value: expiryText,
                        color: expiryColor,
                      ),
                    ),
                ],
              ),
              if (quota?.usedFraction != null) ...[
                const SizedBox(height: 10),
                Builder(builder: (context) {
                  final q = quota!;
                  final fraction = q.usedFraction!;
                  final nearCap = fraction >= 0.9;
                  final color = nearCap
                      ? theme.colorScheme.error
                      : theme.colorScheme.primary;
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      ClipRRect(
                        borderRadius: BorderRadius.circular(4),
                        child: LinearProgressIndicator(
                          value: fraction,
                          minHeight: 6,
                          color: color,
                          backgroundColor:
                              theme.colorScheme.surfaceContainerHighest,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        '${q.usedGb.toStringAsFixed(1)} of '
                        '${q.allowanceGb!.toStringAsFixed(0)} GB used',
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: nearCap
                              ? theme.colorScheme.error
                              : theme.colorScheme.outline,
                        ),
                      ),
                    ],
                  );
                }),
              ],
              const SizedBox(height: 8),
              Builder(builder: (context) {
                // Suspended/stopped actions come only from service-status. A
                // status string alone is never treated as proof that payment
                // will reactivate service.
                final serverAction = action;
                final showContractRenewal = serverAction == null &&
                    s.isActive &&
                    days != null &&
                    days >= 0 &&
                    days <= 3;
                final showAction = serverAction != null || showContractRenewal;
                return Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    if (showAction)
                      Expanded(
                        child: FilledButton.icon(
                          icon: Icon(
                            serverAction?.kind == 'contact_support'
                                ? Icons.support_agent_outlined
                                : Icons.payment,
                            size: 18,
                          ),
                          onPressed: serverAction == null
                              ? () => context.go('/billing')
                              : () => _openServiceAction(context, serverAction),
                          label: Text(serverAction?.label ?? 'Renew'),
                        ),
                      ),
                    if (showAction) const SizedBox(width: 8),
                    TextButton(
                      onPressed: () =>
                          context.push('/service/${s.id}', extra: s),
                      child: const Text('Manage'),
                    ),
                  ],
                );
              }),
            ],
          ),
        ),
      ),
    );
  }
}

class _MiniStat extends StatelessWidget {
  const _MiniStat({
    required this.icon,
    required this.label,
    required this.value,
    this.color,
  });
  final IconData icon;
  final String label;
  final String value;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      children: [
        Icon(icon, size: 18, color: color ?? theme.colorScheme.outline),
        const SizedBox(width: 8),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(label, style: theme.textTheme.labelSmall),
            Text(value,
                style: TextStyle(fontWeight: FontWeight.w600, color: color)),
          ],
        ),
      ],
    );
  }
}

class _SectionHeader extends StatelessWidget {
  const _SectionHeader(this.title);
  final String title;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Text(title, style: Theme.of(context).textTheme.titleMedium),
    );
  }
}

class _MessageCard extends StatelessWidget {
  const _MessageCard(this.message);
  final String message;
  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Text(message),
        ),
      );
}
