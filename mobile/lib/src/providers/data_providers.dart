import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/invoice.dart';
import '../models/ledger.dart';
import '../models/notification.dart';
import '../models/payment_method.dart';
import '../models/session.dart';
import '../models/page.dart';
import '../models/subscription.dart';
import '../models/ticket.dart';
import '../models/usage.dart';
import '../repositories/billing_repository.dart';
import '../repositories/catalog_repository.dart';
import '../repositories/notification_repository.dart';
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

final notificationRepositoryProvider = Provider<NotificationRepository>(
    (ref) => NotificationRepository(ref.watch(apiClientProvider).dio));

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

final subscriptionsProvider =
    FutureProvider.autoDispose<Page<Subscription>>((ref) async {
  cacheFor(ref);
  return ref.watch(catalogRepositoryProvider).subscriptions();
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
