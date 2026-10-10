import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_client.dart';
import '../auth/auth_state.dart';

/// Immutable identity projection of the canonical auth MeResponse.
/// Organization and vendor role are unavailable from that contract.
class VendorProfile {
  const VendorProfile({
    required this.name,
    this.email,
    this.vendorName,
    this.vendorRole,
  });

  final String name;
  final String? email;
  final String? vendorName;
  final String? vendorRole;

  factory VendorProfile.fromJson(Object? payload) {
    if (payload is! Map<String, Object?>) {
      throw const FormatException('Invalid profile response');
    }
    String requiredString(String field) {
      final value = payload[field];
      if (value is! String) {
        throw FormatException('Invalid profile field: $field');
      }
      return value.trim();
    }

    final firstName = requiredString('first_name');
    final lastName = requiredString('last_name');
    final email = requiredString('email');
    final displayName = payload['display_name'];
    if (displayName != null && displayName is! String) {
      throw const FormatException('Invalid profile field: display_name');
    }
    final preferredName = (displayName as String?)?.trim();
    final name = preferredName != null && preferredName.isNotEmpty
        ? preferredName
        : [firstName, lastName].where((part) => part.isNotEmpty).join(' ');
    if (name.isEmpty || email.isEmpty) {
      throw const FormatException('Profile identity is unavailable');
    }
    return VendorProfile(name: name, email: email);
  }
}

class VendorProfileRepository {
  const VendorProfileRepository(this._api);

  final ApiClient _api;

  Future<VendorProfile> fetchMe() async {
    final response = await _api.dio.get<Object?>('/api/v1/auth/me');
    return VendorProfile.fromJson(response.data);
  }
}

final vendorProfileRepositoryProvider = Provider<VendorProfileRepository>(
  (ref) => VendorProfileRepository(ref.watch(apiClientProvider)),
);

final vendorProfileProvider = FutureProvider<VendorProfile>((ref) {
  ref.watch(authControllerProvider);
  return ref.watch(vendorProfileRepositoryProvider).fetchMe();
});
