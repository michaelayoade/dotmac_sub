import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/invoice.dart';
import '../models/ledger.dart';
import '../models/notification.dart';
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

// --- Data providers (all self-scoped to the signed-in subscriber) -----------

final invoicesProvider = FutureProvider.autoDispose<Page<Invoice>>((ref) async {
  return ref.watch(billingRepositoryProvider).invoices();
});

final invoiceProvider =
    FutureProvider.autoDispose.family<Invoice, String>((ref, id) async {
  return ref.watch(billingRepositoryProvider).invoice(id);
});

final paymentsProvider = FutureProvider.autoDispose<Page<Payment>>((ref) async {
  return ref.watch(billingRepositoryProvider).payments();
});

final ledgerProvider = FutureProvider.autoDispose<Page<LedgerTxn>>((ref) async {
  return ref.watch(billingRepositoryProvider).ledger();
});

final subscriptionsProvider =
    FutureProvider.autoDispose<Page<Subscription>>((ref) async {
  return ref.watch(catalogRepositoryProvider).subscriptions();
});

/// The customer's single *current* service: prefer an active subscription, then
/// the most recently started. Null when there are none. (Customers care about
/// their live service, not the historical list.)
final currentServiceProvider =
    Provider.autoDispose<AsyncValue<Subscription?>>((ref) {
  return ref.watch(subscriptionsProvider).whenData((page) {
    if (page.items.isEmpty) return null;
    final sorted = [...page.items]..sort((a, b) {
        if (a.isActive != b.isActive) return a.isActive ? -1 : 1;
        final ad = a.startAt ?? DateTime.fromMillisecondsSinceEpoch(0);
        final bd = b.startAt ?? DateTime.fromMillisecondsSinceEpoch(0);
        return bd.compareTo(ad);
      });
    return sorted.first;
  });
});

/// All quota buckets for the subscriber, in a single round-trip.
final quotaBucketsProvider =
    FutureProvider.autoDispose<List<QuotaBucket>>((ref) async {
  final page = await ref.watch(usageRepositoryProvider).quotaBuckets();
  return page.items;
});

/// The subscriber's RADIUS accounting (data-usage) sessions.
final accountingSessionsProvider =
    FutureProvider.autoDispose<Page<AccountingSession>>((ref) async {
  return ref.watch(usageRepositoryProvider).sessions();
});

final sessionsProvider =
    FutureProvider.autoDispose<List<AuthSessionInfo>>((ref) async {
  return ref.watch(authRepositoryProvider).sessions();
});

final notificationsProvider =
    FutureProvider.autoDispose<Page<AppNotification>>((ref) async {
  return ref.watch(notificationRepositoryProvider).list();
});

final ticketsProvider = FutureProvider.autoDispose<Page<Ticket>>((ref) async {
  return ref.watch(supportRepositoryProvider).tickets();
});

final ticketProvider =
    FutureProvider.autoDispose.family<Ticket, String>((ref, id) async {
  return ref.watch(supportRepositoryProvider).ticket(id);
});

final ticketCommentsProvider = FutureProvider.autoDispose
    .family<Page<TicketComment>, String>((ref, ticketId) async {
  return ref.watch(supportRepositoryProvider).comments(ticketId);
});
