import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/formatters.dart';
import '../../models/addon.dart';
import '../../models/subscription.dart';
import '../../models/usage.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';
import '../../widgets/skeleton.dart';
import '../usage/fup_card.dart';
import '../usage/usage_section.dart';

/// The Service tab — one coherent home for everything about the customer's
/// plan: status header (with multi-service switcher), data balance + FUP
/// state, active add-ons/bundles, upgrade/buy-data actions, and the usage
/// chart + sessions that used to be the Usage tab.
class ServiceTabScreen extends ConsumerWidget {
  const ServiceTabScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final period = ref.watch(selectedUsagePeriodProvider);
    final summary = ref.watch(usageSummaryProvider(period));
    final buckets = ref.watch(quotaBucketsProvider);
    final sessions = ref.watch(accountingSessionsProvider);
    final services = ref.watch(subscriptionsProvider);
    final displayed = ref.watch(displayedServiceProvider);

    final service = displayed.asData?.value;
    final addons = service == null
        ? const AsyncValue<AddonsAvailable>.loading()
        : ref.watch(addonsProvider(service.id));
    final canBuyData =
        addons.asData?.value.available.any((o) => o.isDataTopup) ?? false;
    final activeBundles = (addons.asData?.value.active ?? const <ActiveAddon>[])
        .where((a) => !a.isExpired)
        .toList();

    final allBuckets = buckets.asData?.value ?? const <QuotaBucket>[];
    final serviceBuckets = service == null
        ? allBuckets
        : allBuckets.where((b) => b.subscriptionId == service.id).toList();
    final sessionList =
        sessions.asData?.value.items ?? const <AccountingSession>[];
    final serviceCount = services.asData?.value.items.length ?? 0;

    return Scaffold(
      appBar: AppBar(title: const Text('Service')),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(usageSummaryProvider(period));
          ref.invalidate(quotaBucketsProvider);
          ref.invalidate(usageHistoryProvider);
          ref.invalidate(accountingSessionsProvider);
          ref.invalidate(subscriptionsProvider);
          if (service != null) ref.invalidate(addonsProvider(service.id));
          await ref.read(usageSummaryProvider(period).future);
        },
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (service != null) ...[
              _ServiceHeader(
                service: service,
                showSwitcher: serviceCount > 1,
                onSwitch: (id) =>
                    ref.read(selectedServiceIdProvider.notifier).state = id,
                services: services.asData?.value.items ?? const [],
              ),
              const SizedBox(height: 12),
            ],
            AsyncValueView(
              value: summary,
              onRetry: () => ref.invalidate(usageSummaryProvider(period)),
              skeleton: const CardSkeleton(height: 120),
              data: (s) {
                final fup = s.fup;
                final show = fup != null &&
                    (fup.needsAttention ||
                        fup.isApproaching ||
                        fup.policySummary != null);
                if (!show) return const SizedBox.shrink();
                return Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (fup.needsAttention || fup.isApproaching) ...[
                      FupCard(
                        fup: fup,
                        serviceId: service?.id,
                        canBuyData: canBuyData,
                      ),
                      const SizedBox(height: 12),
                    ],
                  ],
                );
              },
            ),
            for (final b in serviceBuckets) ...[
              QuotaCard(
                bucket: b,
                policyLine: summary.asData?.value.fup?.policySummary,
              ),
              const SizedBox(height: 12),
            ],
            if (serviceBuckets.isEmpty &&
                (summary.asData?.value.fup?.thresholdGb != null)) ...[
              _FupHeadroomCard(fup: summary.asData!.value.fup!),
              const SizedBox(height: 12),
            ],
            if (service != null && canBuyData) ...[
              Align(
                alignment: Alignment.centerLeft,
                child: FilledButton.tonalIcon(
                  onPressed: () => context.push(
                    '/service/${service.id}/buy-data',
                    extra: service,
                  ),
                  icon: const Icon(Icons.add_chart_outlined, size: 18),
                  label: const Text('Buy data'),
                ),
              ),
              const SizedBox(height: 12),
            ],
            if (activeBundles.isNotEmpty) ...[
              Text('Active add-ons',
                  style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              for (final a in activeBundles) _ActiveAddonTile(addon: a),
              if (service != null)
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton(
                    onPressed: () => context.push(
                      '/service/${service.id}/addons',
                      extra: service,
                    ),
                    child: const Text('Manage add-ons'),
                  ),
                ),
              const SizedBox(height: 8),
            ],
            if (service != null) ...[
              Row(
                children: [
                  Expanded(
                    child: FilledButton.icon(
                      onPressed: () => context.push(
                        '/service/${service.id}/change-plan',
                        extra: service,
                      ),
                      icon: const Icon(Icons.upgrade, size: 18),
                      label: const Text('Upgrade plan'),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: () => context.push(
                        '/service/${service.id}',
                        extra: service,
                      ),
                      icon: const Icon(Icons.tune, size: 18),
                      label: const Text('Manage service'),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 20),
            ],
            const MonthlyUsageCard(),
            UsageSection(
              period: period,
              summary: summary,
              sessions: sessionList,
              onSelectPeriod: (p) =>
                  ref.read(selectedUsagePeriodProvider.notifier).state = p,
              onRetry: () => ref.invalidate(usageSummaryProvider(period)),
            ),
          ],
        ),
      ),
    );
  }
}

class _ServiceHeader extends StatelessWidget {
  const _ServiceHeader({
    required this.service,
    required this.showSwitcher,
    required this.onSwitch,
    required this.services,
  });

  final Subscription service;
  final bool showSwitcher;
  final ValueChanged<String> onSwitch;
  final List<Subscription> services;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final details = [
      service.status,
      if (service.planType != null) service.planType!,
      if (service.ipv4Address != null) 'IP ${service.ipv4Address}',
    ].join(' · ');
    final days = service.daysUntilExpiry;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(service.displayName,
                      style: theme.textTheme.titleMedium
                          ?.copyWith(fontWeight: FontWeight.w600)),
                ),
                if (showSwitcher)
                  PopupMenuButton<String>(
                    icon: const Icon(Icons.unfold_more),
                    tooltip: 'Switch service',
                    onSelected: onSwitch,
                    itemBuilder: (_) => [
                      for (final s in services)
                        PopupMenuItem(
                          value: s.id,
                          child: Text(s.displayName),
                        ),
                    ],
                  ),
              ],
            ),
            const SizedBox(height: 4),
            Text(details,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.outline)),
            // Only show a renewal line for a real, upcoming date-based expiry.
            // An active service with a stale billing date isn't "due"; postpaid
            // has no date expiry. Genuine lapses show "Expired".
            if (service.isExpired) ...[
              const SizedBox(height: 4),
              Text('Expired',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.error)),
            ] else if (days != null && days >= 0) ...[
              const SizedBox(height: 4),
              Text(
                days == 0 ? 'Renews today' : 'Renews in $days days',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.outline),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _ActiveAddonTile extends StatelessWidget {
  const _ActiveAddonTile({required this.addon});
  final ActiveAddon addon;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final a = addon;
    final qty = a.quantity > 1 ? ' ×${a.quantity}' : '';
    final gb = a.totalGrantGb != null ? ' — ${a.totalGrantGb} GB' : '';
    final expiry = a.expiresAt != null
        ? (a.daysLeft != null && a.daysLeft! <= 3
            ? 'expires in ${a.daysLeft} day${a.daysLeft == 1 ? '' : 's'}'
            : 'expires ${Fmt.date(a.expiresAt!)}')
        : 'lasts this billing period';
    final urgent = a.daysLeft != null && a.daysLeft! <= 3;

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        dense: true,
        leading: Icon(
          a.isDataBundle ? Icons.sim_card_outlined : Icons.extension_outlined,
          color: theme.colorScheme.primary,
        ),
        title: Text('${a.name}$qty$gb'),
        subtitle: Text(
          expiry,
          style: theme.textTheme.bodySmall?.copyWith(
            color: urgent ? theme.colorScheme.error : theme.colorScheme.outline,
          ),
        ),
      ),
    );
  }
}

/// Fair-use position for unlimited plans (no quota bucket): how far the
/// customer is from the slowdown, with the policy terms — shown proactively
/// at full speed, not only once "approaching".
class _FupHeadroomCard extends StatelessWidget {
  const _FupHeadroomCard({required this.fup});
  final FupStatus fup;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final used = fup.usedGb ?? 0;
    final threshold = fup.thresholdGb ?? 0;
    final ratio = (fup.usageRatio ?? 0).clamp(0.0, 1.0);
    final warn = ratio >= 0.8;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text('Fair-use allowance', style: theme.textTheme.bodySmall),
                Text('Unlimited',
                    style: theme.textTheme.labelMedium
                        ?.copyWith(color: theme.colorScheme.primary)),
              ],
            ),
            const SizedBox(height: 12),
            Text(
              '${Fmt.gb(used)} / ${Fmt.gb(threshold)} full speed',
              style: theme.textTheme.headlineSmall,
            ),
            const SizedBox(height: 12),
            LinearProgressIndicator(
              value: ratio,
              minHeight: 10,
              borderRadius: BorderRadius.circular(5),
              color: warn ? theme.colorScheme.error : null,
            ),
            if (fup.gbUntilThrottle != null) ...[
              const SizedBox(height: 8),
              Text(
                '${Fmt.gb(fup.gbUntilThrottle!)} until reduced speed',
                style: theme.textTheme.bodySmall?.copyWith(
                  color: warn
                      ? theme.colorScheme.error
                      : theme.colorScheme.outline,
                ),
              ),
            ],
            if (fup.policySummary != null) ...[
              const SizedBox(height: 8),
              Row(
                children: [
                  Icon(Icons.info_outline,
                      size: 14, color: theme.colorScheme.outline),
                  const SizedBox(width: 4),
                  Expanded(
                    child: Text(
                      fup.policySummary!,
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.outline),
                    ),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}
