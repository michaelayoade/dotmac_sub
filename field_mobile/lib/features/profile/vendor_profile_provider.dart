import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/auth_state.dart';

class VendorProfile {
  const VendorProfile({
    required this.name,
    required this.vendorName,
    this.email,
    this.vendorRole,
  });

  final String name;
  final String vendorName;
  final String? email;
  final String? vendorRole;

  factory VendorProfile.fromJson(Map<String, dynamic> json) => VendorProfile(
    name: json['name'] as String? ?? '',
    vendorName: json['vendor_name'] as String? ?? '',
    email: json['email'] as String?,
    vendorRole: json['vendor_role'] as String?,
  );
}

class VendorProfileRepository {
  const VendorProfileRepository(this._ref);

  final Ref _ref;

  Future<VendorProfile> fetchMe() async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get('/api/v1/vendor/auth/me');
    return VendorProfile.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
  }
}

final vendorProfileRepositoryProvider = Provider<VendorProfileRepository>(
  VendorProfileRepository.new,
);

final vendorProfileProvider = FutureProvider<VendorProfile>((ref) {
  ref.watch(authControllerProvider);
  return ref.watch(vendorProfileRepositoryProvider).fetchMe();
});
