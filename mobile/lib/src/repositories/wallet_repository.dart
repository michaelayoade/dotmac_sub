import 'package:dio/dio.dart';

import '../core/api_exception.dart';
import '../core/http.dart';
import '../models/wallet.dart';

/// Wraps the self-scoped VAS wallet endpoints (app/api/me.py, /me/wallet*).
///
/// The whole feature is server-flagged: every endpoint 404s while
/// vas.enabled is off — [overviewOrNull] maps that to null so the UI can
/// hide wallet surfaces without version-gating the app.
class WalletRepository {
  WalletRepository(this.dio);

  final Dio dio;

  /// GET /me/wallet — null when the feature is disabled server-side.
  Future<WalletOverview?> overviewOrNull() async {
    try {
      final data = await guard(() => dio.get('/me/wallet'));
      return WalletOverview.fromJson(data as Map<String, dynamic>);
    } on ApiException catch (e) {
      if (e.statusCode == 404) return null;
      rethrow;
    }
  }

  /// POST /me/wallet/topup/initiate
  Future<WalletTopupInitiation> initiateTopup(double amount) async {
    final data = await guard(
        () => dio.post('/me/wallet/topup/initiate', data: {'amount': amount}));
    return WalletTopupInitiation.fromJson(data as Map<String, dynamic>);
  }

  /// POST /me/wallet/topup/verify — returns the new balance.
  Future<double> verifyTopup(String reference) async {
    final data = await guard(() =>
        dio.post('/me/wallet/topup/verify', data: {'reference': reference}));
    return double.tryParse(
            (data as Map<String, dynamic>)['balance'].toString()) ??
        0;
  }

  /// POST /me/wallet/pay-bill — returns the new balance.
  Future<double> payBill(double amount) async {
    final data = await guard(
        () => dio.post('/me/wallet/pay-bill', data: {'amount': amount}));
    return double.tryParse(
            (data as Map<String, dynamic>)['balance'].toString()) ??
        0;
  }

  /// PATCH /me/wallet/auto-deduct
  Future<WalletOverview> setAutoDeduct(bool enabled) async {
    final data = await guard(
        () => dio.patch('/me/wallet/auto-deduct', data: {'enabled': enabled}));
    return WalletOverview.fromJson(data as Map<String, dynamic>);
  }
}
