import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth_controller.dart';
import 'data_providers.dart';

/// Active reseller "view as customer" session, if any.
class ImpersonationState {
  ImpersonationState({
    required this.customerName,
    required this.accountId,
    required this.expiresAt,
  });

  final String customerName;
  final String accountId;
  final DateTime? expiresAt;
}

/// Drives the customer-mode override: while active, every API call carries
/// the short-lived read-only customer token (see ApiClient.impersonationToken)
/// and the shell shows a persistent banner with an explicit exit.
class ImpersonationController extends Notifier<ImpersonationState?> {
  @override
  ImpersonationState? build() => null;

  Future<ImpersonationState> start(String accountId) async {
    final grant =
        await ref.read(resellerRepositoryProvider).impersonate(accountId);
    ref.read(apiClientProvider).impersonationToken = grant.accessToken;
    final s = ImpersonationState(
      customerName: grant.customerName,
      accountId: grant.accountId,
      expiresAt: grant.expiresAt,
    );
    state = s;
    _refreshCustomerData();
    return s;
  }

  void stop() {
    ref.read(apiClientProvider).impersonationToken = null;
    state = null;
    _refreshCustomerData();
  }

  /// Cached customer-scope data must not leak across identities.
  void _refreshCustomerData() {
    ref.invalidate(subscriptionsProvider);
    ref.invalidate(invoicesProvider);
    ref.invalidate(quotaBucketsProvider);
    ref.invalidate(accountingSessionsProvider);
    ref.invalidate(usageSummaryProvider);
    ref.invalidate(notificationsProvider);
    ref.invalidate(addonsProvider);
  }
}

final impersonationProvider =
    NotifierProvider<ImpersonationController, ImpersonationState?>(
        ImpersonationController.new);
