import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/addon.dart';
import '../models/contact.dart';
import '../models/invoice.dart';
import '../models/service_status.dart';
import '../models/ledger.dart';
import '../models/notification.dart';
import '../models/payment_method.dart';
import '../models/payment_proof.dart';
import '../models/session.dart';
import '../models/page.dart';
import '../models/subscription.dart';
import '../models/ticket.dart';
import '../models/usage.dart';
import '../repositories/billing_repository.dart';
import '../repositories/catalog_repository.dart';
import '../repositories/contact_repository.dart';
import '../models/reseller.dart';
import '../models/service_location.dart';
import '../repositories/location_repository.dart';
import '../models/vas.dart';
import '../models/wallet.dart';
import '../repositories/notification_repository.dart';
import '../repositories/wallet_repository.dart';
import '../repositories/reseller_repository.dart';
import '../repositories/support_repository.dart';
import '../repositories/usage_repository.dart';
import 'auth_controller.dart';

// --- Repository providers ---------------------------------------------------

final billingRepositoryProvider = Provider<BillingRepository>(
    (ref) => BillingRepository(ref.watch(apiClientProvider).dio));

final usageRepositoryProvider = Provider<UsageRepository>(
    (ref) => UsageRepository(ref.watch(apiClientProvider).dio));

final catalogRepositoryProvider = Provider<CatalogRepository>(
    (ref) => CatalogRepository(ref.watch(apiClientProvider).dio));

final supportRepositoryProvider = Provider<SupportRepository>(
    (ref) => SupportRepository(ref.watch(apiClientProvider).dio));

final locationRepositoryProvider = Provider<LocationRepository>(
    (ref) => LocationRepository(ref.watch(apiClientProvider).dio));
final walletRepositoryProvider = Provider<WalletRepository>(
    (ref) => WalletRepository(ref.watch(apiClientProvider).dio));

final notificationRepositoryProvider = Provider<NotificationRepository>(
    (ref) => NotificationRepository(ref.watch(apiClientProvider).dio));

final contactRepositoryProvider = Provider<ContactRepository>(
    (ref) => ContactRepository(ref.watch(apiClientProvider).dio));

final resellerRepositoryProvider = Provider<ResellerRepository>(
    (ref) => ResellerRepository(ref.watch(apiClientProvider).dio));

/// The authenticated reseller's dashboard (KPIs + first page of accounts).
/// Portfolio aggregation is a heavy call, so cache it stale-while-revalidate.
final resellerDashboardProvider =
    FutureProvider.autoDispose<ResellerDashboard>((ref) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).dashboard();
});

/// 12-month revenue summary for the reseller portal.
final resellerRevenueProvider =
    FutureProvider.autoDispose<ResellerRevenue>((ref) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).revenue();
});

/// One managed account's detail (subscriptions + open balance).
final resellerAccountProvider = FutureProvider.autoDispose
    .family<ResellerAccountDetail, String>((ref, accountId) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).account(accountId);
});

/// Invoices for one managed account.
final resellerAccountInvoicesProvider = FutureProvider.autoDispose
    .family<List<ResellerInvoiceSummary>, String>((ref, accountId) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).accountInvoices(accountId);
});

/// Reseller organization profile + MFA state.
final resellerProfileProvider =
    FutureProvider.autoDispose<ResellerProfile>((ref) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).profile();
});

/// Consolidated billing statement for the reseller portal.
final resellerBillingProvider =
    FutureProvider.autoDispose<ResellerBillingSummary>((ref) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).billing();
});

/// The reseller's saved cards (GET /reseller/payment-methods). Invalidate after
/// set-default / remove / a save-card payment.
final resellerPaymentMethodsProvider =
    FutureProvider.autoDispose<List<SavedCard>>((ref) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).paymentMethods();
});

/// Fiber-plant map for the reseller coverage screen.
final resellerFiberMapProvider =
    FutureProvider.autoDispose<ResellerFiberMap>((ref) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).fiberMap();
});

/// The reseller's submitted service requests.
final resellerServiceRequestsProvider =
    FutureProvider.autoDispose<List<ResellerServiceRequest>>((ref) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).serviceRequests();
});

/// CRM tickets for one managed account (reseller portal).
final resellerAccountTicketsProvider = FutureProvider.autoDispose
    .family<ResellerTicketsPage, String>((ref, accountId) async {
  cacheFor(ref);
  return ref.watch(resellerRepositoryProvider).accountTickets(accountId);
});

/// The signed-in subscriber's id (== Subscriber.id == billing account_id).
/// Used where a request needs the caller's id explicitly (e.g. new tickets);
/// the data lists below are self-scoped server-side via the /me/* endpoints.
final accountIdProvider = Provider<String?>((ref) {
  return ref.watch(currentUserProvider)?.id;
});

// --- Caching ----------------------------------------------------------------

/// Keep an `autoDispose` provider's value cached for [ttl] after its last
/// listener detaches, instead of discarding it immediately. Revisiting a screen
/// within the window renders cached data instantly (stale-while-revalidate)
/// rather than dropping to a bare spinner and refetching. The data still
/// refreshes on pull-to-refresh / explicit invalidation; this only governs how
/// long an unwatched result survives in memory.
void cacheFor(Ref ref, [Duration ttl = const Duration(minutes: 5)]) {
  final link = ref.keepAlive();
  Timer? timer;
  ref.onDispose(() => timer?.cancel());
  ref.onCancel(() => timer = Timer(ttl, link.close));
  ref.onResume(() => timer?.cancel());
}

// --- Data providers (all self-scoped to the signed-in subscriber) -----------

final invoicesProvider = FutureProvider.autoDispose<Page<Invoice>>((ref) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).invoices();
});

final invoiceProvider =
    FutureProvider.autoDispose.family<Invoice, String>((ref, id) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).invoice(id);
});

final paymentsProvider = FutureProvider.autoDispose<Page<Payment>>((ref) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).payments();
});

final ledgerProvider = FutureProvider.autoDispose<Page<LedgerTxn>>((ref) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).ledger();
});

final balanceProvider = FutureProvider.autoDispose<AccountBalance>((ref) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).balance();
});

final paymentMethodsProvider =
    FutureProvider.autoDispose<List<SavedCard>>((ref) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).paymentMethods();
});

final autopayStatusProvider =
    FutureProvider.autoDispose<AutopayStatus>((ref) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).autopayStatus();
});

/// Truthful account/service health (GET /me/service-status): balance, grace,
/// deactivation, dunning. Drives the renew/top-up banner with the real cut date
/// instead of guessing from a billing date.
final serviceStatusProvider =
    FutureProvider.autoDispose<ServiceStatus>((ref) async {
  cacheFor(ref);
  return ref.watch(catalogRepositoryProvider).serviceStatus();
});

final subscriptionsProvider =
    FutureProvider.autoDispose<Page<Subscription>>((ref) async {
  cacheFor(ref);
  final page = await ref.watch(catalogRepositoryProvider).subscriptions();
  // The API returns the subscriber's full history, including terminated
  // plans (disabled/canceled/hidden). The app only ever presents
  // operationally-current services, so drop the rest here — otherwise a
  // historical plan haunts the dashboard switcher and trips the
  // "service suspended" banner forever.
  final current = page.items.where((s) => s.isCurrent).toList();
  return Page(
    items: current,
    count: current.length,
    limit: page.limit,
    offset: page.offset,
  );
});

/// The customer's single *current* service: prefer an active subscription, then
/// the most recently started. Null when there are none. (Customers care about
/// their live service, not the historical list.)
final currentServiceProvider =
    Provider.autoDispose<AsyncValue<Subscription?>>((ref) {
  return ref.watch(subscriptionsProvider).whenData((page) {
    if (page.items.isEmpty) return null;
    return pickCurrentService(page.items);
  });
});

/// Shared selection rule for "the customer's current service": prefer an active
/// subscription, then the most recently started. Used by both
/// [currentServiceProvider] and the dashboard service switcher so the default
/// selection matches across the app.
Subscription pickCurrentService(List<Subscription> services) {
  final sorted = [...services]..sort((a, b) {
      if (a.isActive != b.isActive) return a.isActive ? -1 : 1;
      final ad = a.startAt ?? DateTime.fromMillisecondsSinceEpoch(0);
      final bd = b.startAt ?? DateTime.fromMillisecondsSinceEpoch(0);
      return bd.compareTo(ad);
    });
  return sorted.first;
}

/// Subscription the dashboard "Current service" card shows. Null = follow
/// [pickCurrentService]; set to a subscription id when the user picks one from
/// the switcher. Kept alive so the choice survives leaving and returning.
final selectedServiceIdProvider = StateProvider.autoDispose<String?>((ref) {
  cacheFor(ref);
  return null;
});

/// The service the dashboard card and the Service tab display: the user's
/// switcher pick when set, else the shared current-service rule.
final displayedServiceProvider =
    Provider.autoDispose<AsyncValue<Subscription?>>((ref) {
  final selectedId = ref.watch(selectedServiceIdProvider);
  return ref.watch(subscriptionsProvider).whenData((page) {
    if (page.items.isEmpty) return null;
    if (selectedId != null) {
      for (final s in page.items) {
        if (s.id == selectedId) return s;
      }
    }
    return pickCurrentService(page.items);
  });
});

/// Add-ons (buyable options + active purchases) for a subscription. Drives the
/// plan-conditional "Buy data" entry points and the active-bundles section.
final addonsProvider = FutureProvider.autoDispose
    .family<AddonsAvailable, String>((ref, subscriptionId) async {
  cacheFor(ref);
  return ref.watch(catalogRepositoryProvider).addons(subscriptionId);
});

/// How the Invoices tab list is filtered.
enum InvoiceFilter {
  all('All'),
  unpaid('Unpaid'),
  overdue('Overdue'),
  paid('Paid');

  const InvoiceFilter(this.label);
  final String label;

  bool test(Invoice inv) => switch (this) {
        InvoiceFilter.all => true,
        InvoiceFilter.unpaid => !inv.isPaid,
        InvoiceFilter.overdue => inv.isOverdue,
        InvoiceFilter.paid => inv.isPaid,
      };
}

final invoiceFilterProvider = StateProvider.autoDispose<InvoiceFilter>((ref) {
  cacheFor(ref);
  return InvoiceFilter.all;
});

/// My bank-transfer payment proofs (pending + reviewed).
final paymentProofsProvider =
    FutureProvider.autoDispose<List<PaymentProofItem>>((ref) async {
  cacheFor(ref);
  return ref.watch(billingRepositoryProvider).myPaymentProofs();
});

/// All quota buckets for the subscriber, in a single round-trip.
final quotaBucketsProvider =
    FutureProvider.autoDispose<List<QuotaBucket>>((ref) async {
  cacheFor(ref);
  final page = await ref.watch(usageRepositoryProvider).quotaBuckets();
  return page.items;
});

/// The subscriber's RADIUS accounting (data-usage) sessions.
final accountingSessionsProvider =
    FutureProvider.autoDispose<Page<AccountingSession>>((ref) async {
  cacheFor(ref);
  return ref.watch(usageRepositoryProvider).sessions();
});

/// Selected window for the Usage tab summary (hour|today|week|cycle|all).
final selectedUsagePeriodProvider = StateProvider.autoDispose<String>((ref) {
  cacheFor(ref);
  return 'today';
});

/// Windowed data-usage summary for a given period.
final usageSummaryProvider = FutureProvider.autoDispose
    .family<UsageSummary, String>((ref, period) async {
  cacheFor(ref);
  return ref.watch(usageRepositoryProvider).usageSummary(period);
});

/// Selected look-back window (days) for the long-history usage chart.
/// 365 = 1Y, 730 = 2Y, 3660 = full archive (the endpoint's max).
final usageHistoryDaysProvider = StateProvider.autoDispose<int>((ref) {
  cacheFor(ref);
  return 365;
});

/// Long-history daily usage (GET /me/usage-history), aggregated to months in
/// the UI. Keyed by the look-back window in days.
final usageHistoryProvider =
    FutureProvider.autoDispose.family<UsageHistory, int>((ref, days) async {
  cacheFor(ref);
  return ref.watch(usageRepositoryProvider).usageHistory(days: days);
});

/// Selected range (hours back) for the speed-history chart.
/// 1h / 6h / 24h / 7d / 30d.
final speedRangeHoursProvider = StateProvider.autoDispose<int>((ref) {
  cacheFor(ref);
  return 24;
});

/// Bandwidth-speed time series (GET /bandwidth/my/series) over the selected
/// look-back window in hours. VM-backed, so it reaches as far as VM retention.
final bandwidthSeriesProvider =
    FutureProvider.autoDispose.family<List<BandwidthPoint>, int>(
  (ref, hours) async {
    cacheFor(ref);
    final end = DateTime.now();
    final start = end.subtract(Duration(hours: hours));
    return ref
        .watch(usageRepositoryProvider)
        .bandwidthSeries(start: start, end: end);
  },
);

/// Live throughput for the active subscription (connection banner). Streams
/// fresh samples while the dashboard is open; autoDispose stops the poll when
/// it closes. Errors (e.g. no active subscription) surface as a no-signal
/// value, so callers read it via asData and omit the figure.
final liveBandwidthProvider = StreamProvider.autoDispose<LiveBandwidth>((ref) {
  cacheFor(ref);
  return ref.watch(usageRepositoryProvider).liveBandwidthStream();
});

final sessionsProvider =
    FutureProvider.autoDispose<List<AuthSessionInfo>>((ref) async {
  cacheFor(ref);
  return ref.watch(authRepositoryProvider).sessions();
});

final notificationsProvider =
    FutureProvider.autoDispose<Page<AppNotification>>((ref) async {
  cacheFor(ref);
  return ref.watch(notificationRepositoryProvider).list();
});

final serviceLocationProvider =
    FutureProvider.autoDispose<ServiceLocation>((ref) async {
  cacheFor(ref);
  return ref.watch(locationRepositoryProvider).location();
});

final vasCatalogProvider =
    FutureProvider.autoDispose<List<VasCategory>>((ref) async {
  cacheFor(ref);
  return ref.watch(walletRepositoryProvider).catalog();
});

final vasPurchasesProvider =
    FutureProvider.autoDispose<List<VasTransaction>>((ref) async {
  cacheFor(ref);
  return ref.watch(walletRepositoryProvider).purchases();
});

/// Null when the wallet feature is disabled server-side (404) — UI hides.
final walletProvider = FutureProvider.autoDispose<WalletOverview?>((ref) async {
  cacheFor(ref);
  return ref.watch(walletRepositoryProvider).overviewOrNull();
});

/// The subscriber's additional contacts. Invalidate after create/update/delete.
final contactsProvider = FutureProvider.autoDispose<List<Contact>>((ref) async {
  cacheFor(ref);
  return ref.watch(contactRepositoryProvider).list();
});

final ticketsProvider = FutureProvider.autoDispose<Page<Ticket>>((ref) async {
  cacheFor(ref);
  return ref.watch(supportRepositoryProvider).tickets();
});

final ticketProvider =
    FutureProvider.autoDispose.family<Ticket, String>((ref, id) async {
  cacheFor(ref);
  return ref.watch(supportRepositoryProvider).ticket(id);
});

final ticketCommentsProvider = FutureProvider.autoDispose
    .family<Page<TicketComment>, String>((ref, ticketId) async {
  cacheFor(ref);
  return ref.watch(supportRepositoryProvider).comments(ticketId);
});
