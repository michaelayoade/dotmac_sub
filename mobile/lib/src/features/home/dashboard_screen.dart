import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../core/semantic_colors.dart';
import '../../models/service_status.dart';
import '../../models/subscription.dart';
import '../../models/usage.dart';
import '../../providers/auth_controller.dart';
import '../../providers/data_providers.dart';
import '../../providers/read_notifications.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/offline_banner.dart';
import '../../widgets/skeleton.dart';
import '../../widgets/status_chip.dart';

/// Home dashboard: an at-a-glance summary (account status, balance, data,
/// services) plus quick-action shortcuts into the rest of the app.
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
    final readIds = ref.watch(readNotificationsProvider);

    final unread = notifications.asData?.value.items
            .where((n) => !readIds.contains(n.id))
            .length ??
        0;

    // --- Summary values (null while loading) ---
    final subList = subs.asData?.value.items;
    // Services the customer can fix by paying (blocked/suspended). The
    // provider already drops terminated plans, so this can't false-alarm
    // on history.
    final needsPayment = subList?.where((s) => s.needsPayment).toList() ??
        const <Subscription>[];

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
    // Total data used this subscription period = RADIUS session octets over the
    // cycle window (server-authoritative). Until that's deployed, fall back to
    // summing the loaded sessions started in the window, then the (retention-
    // limited) throughput series, then today — so it's never a false 0 and never
    // the under-counted series when session data is available.
    int? dataPeriod;
    if (cycleSummary != null) {
      final cycleStart = cycleSummary.start;
      final sessionSum = (sessItems ?? const <AccountingSession>[])
          .where((s) =>
              s.sessionStart != null && !s.sessionStart!.isBefore(cycleStart))
          .fold<int>(0, (a, s) => a + s.totalOctets);
      final seriesSum = cycleSummary.series.fold<int>(0, (a, p) => a + p.bytes);
      dataPeriod = cycleSummary.totalBytes > 0
          ? cycleSummary.totalBytes
          : (sessionSum > 0
              ? sessionSum
              : (seriesSum > 0 ? seriesSum : (dataToday ?? 0)));
    }
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
    if (currentService != null && needsPayment.isEmpty) {
      final name = currentService.displayName;
      if (currentService.isExpired) {
        renewMessage = '$name has expired — renew now';
      } else if (serviceStatus?.needsRenewal ?? false) {
        // The real, balance/dunning-driven nudge: a running service heading for
        // a cut the customer can prevent by paying. The cut date (if known)
        // comes from the prepaid grace timer — never from a billing date.
        renewMessage = _renewFromServiceStatus(serviceStatus!);
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
            const OfflineBanner(),
            _ConnectionBanner(
              session: activeSession,
              known: sessions.hasValue,
              serviceActive: currentService?.isActive ?? false,
              ipAddress: currentService?.ipv4Address,
            ),
            const SizedBox(height: 12),
            _StatusBanner(
              suspendedMessage: _suspendedMessage(
                needsPayment,
                outstanding: outstanding,
                currency: currency,
              ),
              known: subList != null,
              // A blocked/suspended service is resolved by paying — deep-link
              // to billing.
              onTap:
                  needsPayment.isNotEmpty ? () => context.go('/billing') : null,
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
                // Prepaid renewal = top-up; postpaid overdue = pay the bill.
                onTap: () => context.push(
                  (serviceStatus?.isPrepaid ?? true) ? '/topup' : '/billing',
                ),
              ),
            ],
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
                  value: dataPeriod == null ? null : Fmt.bytes(dataPeriod),
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

/// Renewal nudge for a *running* service heading for a cut the customer can
/// prevent by paying — driven by real balance/dunning state, not a billing
/// date. Prepaid surfaces the grace cut-off date when known.
String _renewFromServiceStatus(ServiceStatus s) {
  if (s.isPrepaid) {
    final when = s.graceUntil;
    return when != null
        ? 'Balance low — top up by ${Fmt.date(when)} to keep your service'
        : 'Balance low — top up to keep your service';
  }
  return 'Payment overdue — pay now to avoid suspension';
}

/// Status-banner copy when service(s) are blocked/suspended: names the plan
/// and, when we know the amount due, makes the ask concrete. Null = all good.
String? _suspendedMessage(
  List<Subscription> needsPayment, {
  required double? outstanding,
  required String currency,
}) {
  if (needsPayment.isEmpty) return null;
  final action = (outstanding != null && outstanding > 0)
      ? 'pay ${Fmt.moneyCompact(outstanding, currency)} to restore'
      : 'tap to pay';
  if (needsPayment.length > 1) {
    return '${needsPayment.length} services suspended — $action';
  }
  final service = needsPayment.first;
  final word = service.status == 'blocked' ? 'blocked' : 'suspended';
  return '${service.displayName} $word — $action';
}

/// Network connection status — the headline reason customers open the app.
/// Derived from whether an open RADIUS accounting session exists.
class _ConnectionBanner extends StatelessWidget {
  const _ConnectionBanner({
    required this.session,
    required this.known,
    this.serviceActive = false,
    this.ipAddress,
  });

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

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final Color bg;
    final Color fg;
    final IconData icon;
    final String text;

    if (!known) {
      bg = scheme.surfaceContainerHighest;
      fg = scheme.onSurfaceVariant;
      icon = Icons.wifi_find_outlined;
      text = 'Checking connection…';
    } else if (session != null) {
      final start = session!.sessionStart;
      // The session's framed IP is the live address (covers dynamic plans);
      // the subscription's assigned IP is the fallback.
      final ip = session!.framedIpAddress ?? ipAddress;
      bg = scheme.secondaryContainer;
      fg = scheme.onSecondaryContainer;
      icon = Icons.wifi;
      text = [
        'Connected',
        if (start != null) 'up ${Fmt.uptime(start)}',
        if (ip != null && ip.isNotEmpty) ip,
      ].join(' · ');
    } else if (serviceActive) {
      bg = scheme.surfaceContainerHighest;
      fg = scheme.onSurfaceVariant;
      icon = Icons.wifi_off_outlined;
      text = 'Not connected — service is active, check your router';
    } else {
      bg = scheme.errorContainer;
      fg = scheme.onErrorContainer;
      icon = Icons.wifi_off_outlined;
      text = 'Offline';
    }

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(
        children: [
          Icon(icon, color: fg),
          const SizedBox(width: 10),
          Expanded(
            child: Text(text,
                style: TextStyle(color: fg, fontWeight: FontWeight.w600)),
          ),
        ],
      ),
    );
  }
}

class _StatusBanner extends StatelessWidget {
  const _StatusBanner({
    required this.suspendedMessage,
    required this.known,
    this.onTap,
  });

  /// Concrete attention message ("Unlimited Lite blocked — pay ₦5k to
  /// restore"); null when every service is in good standing.
  final String? suspendedMessage;
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
        : suspendedMessage != null
            ? (
                scheme.errorContainer,
                scheme.onErrorContainer,
                Icons.warning_amber_rounded,
                suspendedMessage!
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
  const _CurrentServiceCard({required this.service, this.quota});
  final Subscription service;

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
                // Surface a pay CTA when the service needs attention: suspended,
                // or expiring within 3 days / already expired.
                final needsAttention =
                    !s.isActive || (days != null && days <= 3);
                return Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    if (needsAttention)
                      Expanded(
                        child: FilledButton.icon(
                          icon: const Icon(Icons.payment, size: 18),
                          onPressed: () => context.go('/billing'),
                          label: Text(s.isActive ? 'Renew' : 'Reactivate'),
                        ),
                      ),
                    if (needsAttention) const SizedBox(width: 8),
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
